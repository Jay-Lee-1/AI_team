"""모델 호출. 상시 실행 프로그램과 개별 모델 호출을 분리한다.
각 호출은 독립된 새 세션(재개 없음)이라 누적 사용량이 중복 합산되지 않는다."""
import json
import os
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field

from . import settings
from .paths import P
from .safety import kill_tree, popen_kwargs, redact, resolve_executable, safe_env


@dataclass
class RunResult:
    ok: bool
    output: dict = None
    usage: dict = field(default_factory=dict)   # {model: {input, output, cache_read, cache_write, cost}}
    cost_usd: float = None
    session_id: str = None
    result_uuid: str = None
    error_kind: str = None     # rate_limit / usage_limit / auth / budget / timeout / killed / parse / other
    error: str = None
    is_mock: bool = False
    raw: str = ""


_procs = {}
_plock = threading.Lock()


def kill_all():
    with _plock:
        procs = list(_procs.values())
    for p in procs:
        kill_tree(p)


_flag_cache = {}


def cli_command():
    s = settings.load()
    custom = (s.get("claude_command") or "").strip()
    if custom:
        return [custom]
    return resolve_executable("claude")


def supported_flags(exe):
    key = tuple(exe)
    if key not in _flag_cache:
        try:
            out = subprocess.run([*exe, "--help"], capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=60).stdout
        except Exception:
            out = ""
        _flag_cache[key] = out
    return _flag_cache[key]


def classify_error(text, status=None, subtype=None):
    t = (text or "").lower()
    if subtype == "error_max_budget_usd":
        return "budget"
    if status in (429, 529) or "rate limit" in t or "rate_limit" in t or "overloaded" in t:
        return "rate_limit"
    if "usage limit" in t or "limit reached" in t or "out of extra usage" in t:
        return "usage_limit"
    if status in (401, 403) or "invalid api key" in t or "authentication" in t \
            or "please run /login" in t or "not logged in" in t or "oauth" in t and "expired" in t:
        return "auth"
    return "other"


class ClaudeCliRunner:
    name = "claude-cli"

    def run(self, spec):
        exe = cli_command()
        if not exe:
            return RunResult(False, error_kind="other",
                             error="Claude Code CLI를 찾지 못했습니다. README의 설치 안내를 따르세요.")
        helptext = supported_flags(exe)
        s = settings.load()
        env = safe_env()
        if s["auth"].get("mode") == "subscription":
            env.pop("ANTHROPIC_API_KEY", None)   # 구독 인증 사용 시 API 과금으로 새지 않게
        elif s["auth"].get("mode") == "api_key" and not os.environ.get("ANTHROPIC_API_KEY"):
            return RunResult(False, error_kind="auth",
                             error="API 키 방식인데 ANTHROPIC_API_KEY 환경 변수가 없습니다.")
        prompt_dir = P.temp / "prompts"
        prompt_dir.mkdir(parents=True, exist_ok=True)
        sp_file = prompt_dir / f"sys-{spec['task_id']}-{uuid.uuid4().hex[:8]}.txt"
        sp_file.write_text(spec["system_prompt"], encoding="utf-8")
        args = [*exe, "-p", "--output-format", "json", "--model", spec["model"]]
        tools = spec.get("tools") or []
        args += ["--tools", ",".join(tools)]
        if "--restricted" in helptext and tools:
            args.append("--restricted")
        for flag, extra in (("--no-session-persistence", []), ("--strict-mcp-config", []),
                            ("--disable-slash-commands", []),
                            ("--permission-mode", ["dontAsk"]),
                            ("--permission-prompts", ["none"])):
            if flag in helptext:
                args += [flag, *extra]
        if "--max-budget-usd" in helptext and spec.get("max_budget_usd"):
            args += ["--max-budget-usd", f"{spec['max_budget_usd']:.2f}"]
        if "--system-prompt-file" in helptext:
            args += ["--system-prompt-file", str(sp_file)]
        else:
            args += ["--system-prompt", spec["system_prompt"]]
        use_schema = "--json-schema" in helptext
        if use_schema:
            args += ["--json-schema", json.dumps(spec["schema"], ensure_ascii=False)]
        prompt = spec["prompt"]
        if not use_schema:
            prompt += "\n\n출력은 다음 JSON 스키마를 따르는 JSON 하나만 작성하라:\n" + \
                json.dumps(spec["schema"], ensure_ascii=False)
        try:
            proc = subprocess.Popen(args, cwd=str(spec["cwd"]), stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                    encoding="utf-8", errors="replace", env=env, **popen_kwargs())
        except OSError as e:
            sp_file.unlink(missing_ok=True)
            return RunResult(False, error_kind="other", error=f"CLI 실행 실패: {e}")
        with _plock:
            _procs[spec["task_id"]] = proc
        killed = False
        try:
            out, err = proc.communicate(prompt, timeout=spec.get("timeout", 900))
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            out, err = proc.communicate()
            return RunResult(False, error_kind="timeout", error="시간 한도 초과", raw=redact(out))
        finally:
            with _plock:
                _procs.pop(spec["task_id"], None)
            sp_file.unlink(missing_ok=True)
        if proc.returncode is not None and proc.returncode < 0:
            killed = True
        return self._parse(out, err, killed)

    def _parse(self, out, err, killed):
        raw = redact(out or "")
        try:
            data = json.loads(out)
        except (ValueError, TypeError):
            if killed:
                return RunResult(False, error_kind="killed", error="중단됨", raw=raw)
            msg = redact((err or out or "").strip()[-800:])
            return RunResult(False, error_kind=classify_error(msg), error=msg or "응답 없음", raw=raw)
        usage = {}
        for model, u in (data.get("modelUsage") or {}).items():
            usage[model] = {"input": u.get("inputTokens"), "output": u.get("outputTokens"),
                            "cache_read": u.get("cacheReadInputTokens"),
                            "cache_write": u.get("cacheCreationInputTokens"),
                            "cost": u.get("costUSD")}
        res = RunResult(False, usage=usage, cost_usd=data.get("total_cost_usd"),
                        session_id=data.get("session_id"), result_uuid=data.get("uuid"), raw=raw)
        if data.get("is_error") or data.get("subtype") != "success":
            text = str(data.get("result") or "") + " " + str(data.get("subtype") or "")
            res.error_kind = classify_error(text, data.get("api_error_status"), data.get("subtype"))
            res.error = redact(text.strip()[:800]) or "모델 호출 오류"
            return res
        output = data.get("structured_output")
        if output is None:
            output = _extract_json(data.get("result") or "")
        if not isinstance(output, dict):
            res.error_kind, res.error = "parse", "구조화된 결과를 읽지 못했습니다"
            return res
        res.ok, res.output = True, output
        return res


def _extract_json(text):
    text = text.strip()
    if "```" in text:
        chunks = text.split("```")
        for c in chunks[1::2]:
            c = c[4:] if c.startswith("json") else c
            try:
                return json.loads(c)
            except ValueError:
                continue
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except ValueError:
            return None
    return None


class MockRunner:
    """모의 호출. 실제 모델을 부르지 않으며 결과·사용량에 '모의'로 표시된다."""
    name = "mock"

    def __init__(self, delay=1.2):
        self.delay = delay
        self.fail_plan = {}  # 테스트용: {kind: [error_kind, ...]}

    def run(self, spec):
        from . import mock_outputs
        stop_at = time.time() + self.delay
        while time.time() < stop_at:
            if spec.get("cancel") and spec["cancel"].is_set():
                return RunResult(False, error_kind="killed", error="중단됨", is_mock=True)
            time.sleep(0.05)
        plan = self.fail_plan.get(spec["kind"])
        if plan:
            kind = plan.pop(0)
            return RunResult(False, error_kind=kind, error=f"모의 오류: {kind}", is_mock=True)
        out = mock_outputs.generate(spec["kind"], spec["input"])
        return RunResult(True, output=out, usage={"mock": {}}, is_mock=True)
