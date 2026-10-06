"""설정 읽기/쓰기. 작업자(모델)는 이 모듈에 접근할 경로가 없다.
관리 화면(사용자)만 update()를 호출한다."""
import copy
import json
import threading

from .paths import P

_lock = threading.RLock()

# 관리 화면에서 바꿀 수 있는 키. 그 밖의 키(경로·허용 명령 등)는 파일을 직접 수정해야 한다.
EDITABLE = {
    "runner", "auth", "budget", "models", "max_parallel_calls", "decision_wait_hours",
    "maintenance", "analysis", "customer_reply_auto_send", "public_allowed_origins",
}


def _deep_merge(base, patch):
    out = copy.deepcopy(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def defaults():
    path = P.default_settings if P.default_settings.exists() else P.repo_default_settings
    return json.loads(path.read_text(encoding="utf-8"))


def load():
    with _lock:
        base = defaults()
        if P.settings_file.exists():
            try:
                user = json.loads(P.settings_file.read_text(encoding="utf-8"))
                base = _deep_merge(base, user)
            except (OSError, ValueError):
                pass
        return base


def update(patch):
    """사용자 요청으로 설정 변경. 허용 키만, 값 형식 검사 후 저장."""
    bad = [k for k in patch if k not in EDITABLE]
    if bad:
        raise ValueError(f"관리 화면에서 바꿀 수 없는 설정입니다: {', '.join(bad)}")
    _validate(patch)
    with _lock:
        current = {}
        if P.settings_file.exists():
            current = json.loads(P.settings_file.read_text(encoding="utf-8"))
        merged = _deep_merge(current, patch)
        tmp = P.settings_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(P.settings_file)
    return load()


def _num_or_none(v, name):
    if v is None:
        return
    if not isinstance(v, (int, float)) or v < 0:
        raise ValueError(f"{name}은(는) 0 이상의 숫자여야 합니다")


def _validate(patch):
    if "runner" in patch and patch["runner"] not in ("claude-cli", "mock"):
        raise ValueError("runner는 claude-cli 또는 mock 이어야 합니다")
    if "auth" in patch:
        mode = (patch["auth"] or {}).get("mode")
        if mode not in (None, "subscription", "api_key"):
            raise ValueError("인증 방식은 subscription 또는 api_key 입니다")
        if set(patch["auth"]) - {"mode"}:
            raise ValueError("비밀정보는 설정에 저장하지 않습니다. 인증 방식만 고르세요")
    if "budget" in patch:
        for k, v in patch["budget"].items():
            if k not in ("daily_usd", "monthly_usd", "per_call_usd", "daily_tokens",
                         "safety_margin_ratio", "slowdown_ratio"):
                raise ValueError(f"알 수 없는 예산 항목: {k}")
            _num_or_none(v, k)
        r = patch["budget"].get("safety_margin_ratio")
        if r is not None and r > 0.5:
            raise ValueError("안전 여유분은 0.5 이하여야 합니다")
    if "models" in patch:
        for k, v in patch["models"].items():
            if k not in ("planning", "design", "development", "qa", "release"):
                raise ValueError(f"알 수 없는 부서: {k}")
            if not isinstance(v, str) or not v.replace("-", "").replace(".", "").isalnum():
                raise ValueError("모델 이름 형식이 올바르지 않습니다")
    if "max_parallel_calls" in patch:
        v = patch["max_parallel_calls"]
        if not isinstance(v, int) or not 1 <= v <= 3:
            raise ValueError("동시 호출 수는 1~3 입니다")
    if "decision_wait_hours" in patch:
        _num_or_none(patch["decision_wait_hours"], "decision_wait_hours")


def is_paid_ready(s=None):
    """인증과 예산이 정해졌는지. 정해지기 전에는 유료 무인 호출을 하지 않는다."""
    s = s or load()
    return bool(s["auth"].get("mode")) and s["budget"].get("daily_usd") is not None \
        and s["budget"].get("monthly_usd") is not None
