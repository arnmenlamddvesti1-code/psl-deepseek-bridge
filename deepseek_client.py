"""DeepSeek Session Bridge Client.

Взаимодействие с веб-сессией chat.deepseek.com через Playwright Headless Chromium
по сессионному токену и cookie пользователя (БЕЗ Cloudflare WARP).
"""

import asyncio
import hashlib
import json
import logging
import math
import os
import statistics
import tempfile
import time
from typing import Optional, Dict, Any, List, Tuple
from playwright.async_api import async_playwright, Browser, BrowserContext, Page
import json_repair

from prompts import MASTER_PROMPT_CALIBRATED_V2, MASTER_PROMPT_RAW, render_prompt

logger = logging.getLogger("DeepSeekBridge.Client")

try:
    from dotenv import load_dotenv
    _env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(_env_path):
        load_dotenv(_env_path)
except Exception:
    pass

# ── Авторизационные токены по умолчанию ──
_DEFAULT_RAW_TOKEN = "k8kbvUlCMaeRCD3h9xtmC4VB9WeIGVDqppax0ioYhDaMqbPYVCHjsxhZWQ3nHGKR"
_DEFAULT_USER_TOKEN_JSON = '{"value":"k8kbvUlCMaeRCD3h9xtmC4VB9WeIGVDqppax0ioYhDaMqbPYVCHjsxhZWQ3nHGKR","__version":"0"}'
_DEFAULT_SESSION_ID = "92f8f850d7084bcfbc2fd1fbfb1e9227"
_DEFAULT_DEVICE_ID = "bf72ea1a-e951-415f-984a-80336b48dea0"

# ── Чтение конфигурации из окружения ──
_env_token = os.environ.get("DEEPSEEK_USER_TOKEN", "").strip()
if _env_token:
    if not _env_token.startswith("{"):
        USER_TOKEN_RAW = json.dumps({"value": _env_token, "__version": "0"})
    else:
        USER_TOKEN_RAW = _env_token
else:
    USER_TOKEN_RAW = _DEFAULT_USER_TOKEN_JSON

SESSION_ID = os.environ.get("DEEPSEEK_SESSION_ID", _DEFAULT_SESSION_ID).strip()
DEVICE_ID = os.environ.get("DEEPSEEK_DEVICE_ID", _DEFAULT_DEVICE_ID).strip()
DEFAULT_PROMPT_VARIANT = os.getenv("DEEPSEEK_PROMPT_VARIANT", "calibrated").strip().lower()

try:
    DEFAULT_PASSES = int(os.getenv("DEEPSEEK_PASSES", "1"))
except ValueError:
    DEFAULT_PASSES = 1
DEFAULT_PASSES = max(1, min(5, DEFAULT_PASSES))

CACHE_ENABLED = os.getenv("DEEPSEEK_CACHE", "1").strip().lower() not in ("0", "off", "false", "no")
CACHE_DIR = os.getenv(
    "DEEPSEEK_CACHE_DIR",
    os.path.join(tempfile.gettempdir(), "psl_deepseek_cache"),
).strip()
try:
    CACHE_TTL_SEC = int(os.getenv("DEEPSEEK_CACHE_TTL", str(7 * 24 * 3600)))
except ValueError:
    CACHE_TTL_SEC = 7 * 24 * 3600

try:
    PASS_TIMEOUT_SEC = float(os.getenv("DEEPSEEK_PASS_TIMEOUT", "80"))
except ValueError:
    PASS_TIMEOUT_SEC = 80.0
try:
    TOTAL_TIMEOUT_SEC = float(os.getenv("DEEPSEEK_TOTAL_TIMEOUT", "220"))
except ValueError:
    TOTAL_TIMEOUT_SEC = 220.0

# ── Зоны столпов ──
ANGULARITY_ZONE_NAMES = (
    "Jaw Definition",
    "Chin Definition",
    "Cheekbone Prominence",
    "Submental Definition",
    "Cheek Leanness",
)

DIMORPHISM_ZONE_NAMES = (
    "Jaw",
    "Eyes",
    "Cheekbone Prominence",
    "Chin Definition",
    "Facial Hair",
    "Nose (Front)",
    "Eyebrow",
    "Hairline",
    "Lips",
    "Face Shape",
)

FEATURES_ZONE_NAMES = (
    "Acne",
    "Acne Scars",
    "Eczema",
    "Rosacea",
    "Hyperpigmentation",
    "Hypopigmentation",
    "Hair Loss",
    "Thinning Hair",
    "Dry Hair",
    "Nasolabial Folds",
    "Frown Lines",
    "Undereye Wrinkles",
    "Mentolabial Creasing",
    "Platysmal Bands",
    "Forehead Lines",
    "Crow's Feet",
    "Eye Bags",
    "Dark Circles",
    "Eye Protrusion",
    "Undereye Redness",
    "Sclera Yellowing",
    "Scleral Show",
    "Sclera Reddening",
    "Droopy Eyelid",
    "Upper Eyelid Exposure",
    "Eyebrow Density",
    "Eyelashes",
    "Nostril Exposure",
    "Bulbous Nasal Tip",
    "Bloated Nasal Bridge",
    "Eyebrow Color",
)

ZONE_GROUPS = {
    "angularity_zones": ANGULARITY_ZONE_NAMES,
    "dimorphism_zones": DIMORPHISM_ZONE_NAMES,
    "features_zones": FEATURES_ZONE_NAMES,
}


def _select_master_prompt(variant: str = "", sex: str = "male") -> str:
    value = (variant or DEFAULT_PROMPT_VARIANT or "calibrated").strip().lower()
    if value in ("raw", "neutral", "plain"):
        return MASTER_PROMPT_RAW
    return render_prompt(MASTER_PROMPT_CALIBRATED_V2, sex)


def _coerce_zone_map(raw: Any, names: Tuple[str, ...]) -> Optional[Dict[str, float]]:
    if not isinstance(raw, dict):
        return None
    lowered = {str(k).strip().lower(): v for k, v in raw.items()}
    out = {}
    for name in names:
        value = raw.get(name)
        if value is None:
            value = lowered.get(name.lower())
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number):
            return None
        out[name] = round(max(1.0, min(10.0, number)), 2)
    return out


def _normalize_pass(data: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(data, dict):
        return None
    normalized = {}
    for group, names in ZONE_GROUPS.items():
        zones = _coerce_zone_map(data.get(group), names)
        if zones is None:
            return None
        normalized[group] = zones
    return normalized


def _pillar_means(zones: Dict[str, Any], sex: str = "male") -> Dict[str, float]:
    ang = zones["angularity_zones"]
    dim = dict(zones["dimorphism_zones"])
    feat = zones["features_zones"]
    if str(sex).strip().lower().startswith("f"):
        dim.pop("Facial Hair", None)
    return {
        "angularity": round(sum(ang.values()) / len(ang), 2),
        "dimorphism": round(sum(dim.values()) / len(dim), 2),
        "features": round(sum(feat.values()) / len(feat), 2),
    }


def _tier_for(score: float) -> str:
    if score >= 8.5:
        return "Apex / Exceptional"
    if score >= 7.0:
        return "Elite Model Tier"
    if score >= 6.0:
        return "High-Tier Normie / Chadlite"
    if score >= 5.0:
        return "Mid Tier Normie"
    if score >= 3.0:
        return "Low-Tier Normie"
    return "Sub-5 / Recessed"


def _aggregate_passes(passes: List[Dict[str, Any]], sex: str = "male") -> Dict[str, Any]:
    merged = {group: {} for group in ZONE_GROUPS}
    for group, names in ZONE_GROUPS.items():
        for name in names:
            values = [p[group][name] for p in passes if name in p[group]]
            if values:
                merged[group][name] = round(float(statistics.median(values)), 2)

    means = [_pillar_means(p, sex) for p in passes]
    spreads = {}
    for key in ("angularity", "dimorphism", "features"):
        vals = [m[key] for m in means]
        spreads[key] = round(max(vals) - min(vals), 2) if vals else 0.0

    agg = _pillar_means(merged, sex)
    overall = round((agg["angularity"] + agg["dimorphism"] + agg["features"]) / 3.0, 2)
    return {
        "status": "success",
        "angularity_score": agg["angularity"],
        "dimorphism_score": agg["dimorphism"],
        "features_score": agg["features"],
        "overall_score": overall,
        "tier": _tier_for(overall),
        "angularity_zones": merged["angularity_zones"],
        "dimorphism_zones": merged["dimorphism_zones"],
        "features_zones": merged["features_zones"],
        "passes": len(passes),
        "pass_spread": spreads,
    }


# ── Кэширование ──
def _cache_key(img_bytes: bytes, prompt: str, sex: str, passes: int) -> str:
    h = hashlib.sha256()
    h.update(img_bytes)
    h.update(b"\0")
    h.update(prompt.encode("utf-8"))
    h.update(b"\0")
    h.update(str(sex).strip().lower().encode("utf-8"))
    h.update(b"\0")
    h.update(str(passes).encode("ascii"))
    return h.hexdigest()


def _cache_read(key: str) -> Optional[Dict[str, Any]]:
    if not CACHE_ENABLED:
        return None
    path = os.path.join(CACHE_DIR, key[:2], key + ".json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            obj = json.load(fh)
        created = float(obj.get("created") or 0.0)
        if CACHE_TTL_SEC > 0 and time.time() - created > CACHE_TTL_SEC:
            return None
        result = obj.get("result")
        if isinstance(result, dict) and result.get("status") == "success":
            cached = dict(result)
            cached["cached"] = True
            return cached
    except Exception:
        pass
    return None


def _cache_write(key: str, result: Dict[str, Any]) -> None:
    if not CACHE_ENABLED:
        return
    path = os.path.join(CACHE_DIR, key[:2], key + ".json")
    tmp = os.path.join(CACHE_DIR, key[:2], f"{key}.{os.getpid()}.{time.time()}.tmp")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"created": time.time(), "result": result}, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:
        logger.debug(f"DeepSeek cache write skipped: {e}")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


# ── Менеджер браузера Playwright ──
class DeepSeekSessionClient:
    def __init__(self):
        self._lock = asyncio.Lock()
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._is_initialized = False

    async def _init_browser(self) -> Page:
        if self._page is not None and not self._page.is_closed():
            return self._page

        if self._playwright is None:
            self._playwright = await async_playwright().start()

        # 1. Попытка подключения по CDP (если запущен локальный Chrome)
        cdp_url = os.environ.get("DEEPSEEK_CDP_URL", "")
        if cdp_url:
            try:
                self._browser = await self._playwright.chromium.connect_over_cdp(cdp_url)
                logger.info(f"Connected to Chrome over CDP: {cdp_url}")
                ctx = self._browser.contexts[0] if self._browser.contexts else await self._browser.new_context()
                for pg in ctx.pages:
                    if "deepseek.com" in pg.url and not pg.is_closed():
                        self._page = pg
                        return self._page
                self._page = await ctx.new_page()
                await self._page.goto("https://chat.deepseek.com", wait_until="domcontentloaded", timeout=25000)
                return self._page
            except Exception as e_cdp:
                logger.warning(f"CDP connection to {cdp_url} failed ({e_cdp}), falling back to headless launch")

        # 2. Автономный запуск Headless Chromium
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--no-first-run",
            "--no-zygote",
        ]
        
        # Запуск chromium
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=launch_args,
            ignore_default_args=["--enable-automation"],
        )

        self._context = await self._browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
            locale="en-US",
        )

        # Установка сессионной cookie ds_session_id
        if SESSION_ID:
            try:
                await self._context.add_cookies([
                    {
                        "name": "ds_session_id",
                        "value": SESSION_ID,
                        "domain": ".deepseek.com",
                        "path": "/",
                        "httpOnly": False,
                        "secure": True,
                    }
                ])
                logger.info("Session cookie ds_session_id successfully injected")
            except Exception as e_cookie:
                logger.warning(f"Failed to set session cookie: {e_cookie}")

        self._page = await self._context.new_page()

        # Внедрение токена в localStorage до загрузки скриптов
        init_js = f"""
            try {{
                window.localStorage.setItem('userToken', '{USER_TOKEN_RAW}');
                window.localStorage.setItem('deepseek-device-id:chat', '{DEVICE_ID}');
            }} catch(e) {{}}
        """
        await self._page.add_init_script(init_js)

        logger.info("Connecting to chat.deepseek.com...")
        await self._page.goto("https://chat.deepseek.com", wait_until="domcontentloaded", timeout=35000)
        await self._page.wait_for_timeout(2500)

        # Повторное применение localStorage на активной странице
        await self._page.evaluate(f"""() => {{
            try {{
                window.localStorage.setItem('userToken', '{USER_TOKEN_RAW}');
                window.localStorage.setItem('deepseek-device-id:chat', '{DEVICE_ID}');
            }} catch(e) {{}}
        }}""")

        self._is_initialized = True
        logger.info("DeepSeek session page initialized successfully!")
        return self._page

    async def _eval_single_pass(
        self, img_bytes: bytes, prompt_text: str, expect: str = "scoring"
    ) -> Dict[str, Any]:
        page = await self._init_browser()
        fd, temp_img = tempfile.mkstemp(suffix=".jpg", prefix="ds_pass_")
        os.write(fd, img_bytes)
        os.close(fd)

        try:
            # Проверка URL и переход на чистый чат
            if "chat.deepseek.com" not in page.url or "sign_in" in page.url or "login" in page.url:
                await page.goto("https://chat.deepseek.com", wait_until="domcontentloaded", timeout=25000)
                await page.wait_for_timeout(2000)
            else:
                try:
                    new_btn = await page.query_selector('div[class*="new-chat"], button[class*="new-chat"], a[href="/"]')
                    if new_btn:
                        await new_btn.click()
                        await page.wait_for_timeout(1000)
                    else:
                        await page.goto("https://chat.deepseek.com", wait_until="domcontentloaded", timeout=20000)
                        await page.wait_for_timeout(1500)
                except Exception:
                    await page.goto("https://chat.deepseek.com", wait_until="domcontentloaded", timeout=20000)
                    await page.wait_for_timeout(1500)

            # Проверка редиректа на страницу авторизации
            if "sign_in" in page.url or "login" in page.url:
                # Попробуем повторно внедрить токен и перейти
                await page.evaluate(f"""() => {{
                    window.localStorage.setItem('userToken', '{USER_TOKEN_RAW}');
                    window.localStorage.setItem('deepseek-device-id:chat', '{DEVICE_ID}');
                }}""")
                await page.goto("https://chat.deepseek.com", wait_until="domcontentloaded", timeout=20000)
                await page.wait_for_timeout(2000)

            if "sign_in" in page.url or "login" in page.url:
                logger.error("DeepSeek session not authenticated (redirected to /sign_in)")
                return {
                    "status": "error",
                    "code": 40003,
                    "error": "DeepSeek session expired or not authenticated. Update DEEPSEEK_USER_TOKEN or DEEPSEEK_SESSION_ID.",
                }

            # Скрытие модальных окон
            try:
                await page.keyboard.press("Escape")
                await page.evaluate(
                    "() => document.querySelectorAll('.ds-modal-wrapper, .ds-modal-focus-lock').forEach(el => el.remove())"
                )
            except Exception:
                pass

            initial_blocks = await page.evaluate(
                "() => Array.from(document.querySelectorAll('.ds-markdown, pre')).map(el => el.innerText)"
            )

            # Загрузка изображения
            try:
                file_input = await page.wait_for_selector('input[type="file"]', state="attached", timeout=15000)
                await file_input.set_input_files(temp_img)
                await page.wait_for_timeout(2500)
            except Exception as e_upload:
                if "sign_in" in page.url:
                    return {
                        "status": "error",
                        "code": 40003,
                        "error": "DeepSeek session not authenticated during file upload",
                    }
                return {"status": "error", "error": f"Image upload selector timeout: {e_upload}"}

            # Ввод текста промпта
            try:
                ta = await page.wait_for_selector('textarea, [contenteditable="true"]', timeout=15000)
                await ta.click()
            except Exception as e_ta:
                return {"status": "error", "error": f"Textarea selector timeout: {e_ta}"}

            await page.keyboard.insert_text(prompt_text)
            await page.wait_for_timeout(1000)
            await page.keyboard.press("Enter")
            logger.info("Prompt sent to DeepSeek. Awaiting response...")

            is_qa = expect == "point_qa"
            required = ("point_qa",) if is_qa else ("angularity_zones", "dimorphism_zones", "features_zones")
            prev_text = None

            # Ожидание стриминга ответа
            for _ in range(80):
                await page.wait_for_timeout(2500)
                cur_blocks = await page.evaluate(
                    "() => Array.from(document.querySelectorAll('.ds-markdown, pre')).map(el => el.innerText)"
                )
                if len(cur_blocks) <= len(initial_blocks):
                    continue

                last_text = cur_blocks[-1]
                if is_qa:
                    if "point_qa" not in last_text:
                        continue
                elif "angularity" not in last_text.lower():
                    continue

                if not all(k in last_text for k in required):
                    prev_text = last_text
                    continue
                if last_text != prev_text:
                    prev_text = last_text
                    continue

                idx = last_text.find("{")
                last_brace = last_text.rfind("}")
                if idx != -1 and last_brace != -1:
                    data = json_repair.loads(last_text[idx : last_brace + 1])
                    if isinstance(data, dict) and all(k in data for k in required):
                        data["status"] = "success"
                        logger.info("DeepSeek evaluation pass parsed successfully")
                        return data

            return {"status": "error", "error": "DeepSeek response timeout"}

        except Exception as e_eval:
            logger.error(f"Error during DeepSeek evaluation: {e_eval}")
            return {"status": "error", "error": str(e_eval)}
        finally:
            if os.path.exists(temp_img):
                try:
                    os.remove(temp_img)
                except Exception:
                    pass

    async def analyze(
        self,
        img_bytes: bytes,
        prompt_text: str = "",
        sex: str = "male",
        variant: str = "",
        expect: str = "scoring",
        passes: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Основной метод анализа фото с кэшированием и мультипроходом."""
        is_qa = expect == "point_qa"
        effective_prompt = (
            prompt_text
            if (prompt_text.strip() and (is_qa or "angularity_score" in prompt_text or "Angularity" in prompt_text))
            else _select_master_prompt(variant, sex)
        )

        num_passes = 1 if is_qa else (passes or DEFAULT_PASSES)
        cache_key = _cache_key(img_bytes, effective_prompt, sex, num_passes)

        # Проверка кэша
        if not is_qa:
            cached = _cache_read(cache_key)
            if cached:
                logger.info(f"DeepSeek cache hit: overall={cached.get('overall_score')}")
                return cached

        async with self._lock:
            if not is_qa:
                cached = _cache_read(cache_key)
                if cached:
                    return cached

            if is_qa:
                return await self._eval_single_pass(img_bytes, effective_prompt, expect)

            complete: List[Dict[str, Any]] = []
            last_error: Dict[str, Any] = {"status": "error", "error": "No complete pass returned"}
            attempts = 0
            max_attempts = num_passes + 2
            deadline = time.time() + TOTAL_TIMEOUT_SEC

            while attempts < max_attempts and len(complete) < num_passes:
                if time.time() >= deadline:
                    break
                attempts += 1
                try:
                    res = await self._eval_single_pass(img_bytes, effective_prompt, expect)
                except Exception as e:
                    logger.warning(f"Pass {attempts} exception: {e}")
                    last_error = {"status": "error", "error": str(e)}
                    continue

                if not isinstance(res, dict) or res.get("status") != "success":
                    last_error = res if isinstance(res, dict) else last_error
                    if isinstance(res, dict) and res.get("code") == 40003:
                        return res
                    continue

                normalized = _normalize_pass(res)
                if normalized is None:
                    last_error = {"status": "error", "error": "DeepSeek pass missing zones"}
                    continue

                complete.append(normalized)
                if len(complete) < num_passes:
                    await asyncio.sleep(0.5)

            if not complete:
                return last_error

            result = _aggregate_passes(complete, sex)
            _cache_write(cache_key, result)
            return result

    async def close(self):
        """Корректное завершение ресурсов Playwright."""
        try:
            if self._context:
                await self._context.close()
            if self._browser:
                await self._browser.close()
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            pass


# Singleton экземпляр
client = DeepSeekSessionClient()
