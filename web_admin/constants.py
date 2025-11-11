# web_admin/constants.py
"""
后台会话键常量，供 auth.py / deps.py / 其他中间件统一使用，避免循环依赖与魔法字符串。
"""

# 登录态：保存当前管理员的基础信息
# 结构示例：{"username": "admin", "tg_id": 123456789}
SESSION_USER_KEY = "admin_user"

# 二次校验通过标志位（高危操作需要）
# True 表示当前会话在 OTP 有效期内已经通过二次校验
TWOFA_PASSED_KEY = "admin_2fa_ok"

# 二次校验的临时代码（仅存 HMAC，不存明文）
# 后端校验时使用 HMAC(code, ADMIN_SESSION_SECRET) 与此值对比
TWOFA_CODE_KEY = "admin_2fa_code"

# 二次校验口令签发时间（epoch seconds）
# 用于判断验证码是否过期
TWOFA_ISSUED_AT = "admin_2fa_issued_at"

# ---- 通用分页与日期格式常量（新增） ----
PAGE_SIZE_DEFAULT = 30
PAGE_SIZE_MAX = 200

DATE_FMT = "%Y-%m-%d"
DATETIME_FMT = "%Y-%m-%d %H:%M:%S"

# 显式导出，避免 * 引入额外符号
__all__ = [
    "SESSION_USER_KEY",
    "TWOFA_PASSED_KEY",
    "TWOFA_CODE_KEY",
    "TWOFA_ISSUED_AT",
    # 新增导出
    "PAGE_SIZE_DEFAULT",
    "PAGE_SIZE_MAX",
    "DATE_FMT",
    "DATETIME_FMT",
]
