"""修改器的领域常量与规格。

所有数值上限、资源字段和属性名称都只在这里定义。界面、Python 侧校验
以及 Frida 脚本（通过 JSON 注入）都从同一份数据派生，避免多处手写漂移。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ParamSpec:
    key: str
    label: str
    safe_total: int


PARAM_SUM_LIMIT = 2_000_000_000
PARAM_SAFE_TOTAL = 9_990_000
PARAM_ONECLICK_BASE = 10_000

# 顺序必须与游戏 Character.param_ / addParam_ 数组一致。
PARAMS = (
    ParamSpec("hp", "HP", PARAM_SAFE_TOTAL),
    ParamSpec("sp", "SP", PARAM_SAFE_TOTAL),
    ParamSpec("atk", "攻击", PARAM_SAFE_TOTAL),
    ParamSpec("def", "防御", PARAM_SAFE_TOTAL),
    ParamSpec("spd", "速度", PARAM_SAFE_TOTAL),
    ParamSpec("wis", "智慧", PARAM_SUM_LIMIT),
)

PARAM_NAMES = [spec.label for spec in PARAMS]
PARAM_SAFE_LIMITED = tuple(
    spec.label for spec in PARAMS if spec.safe_total == PARAM_SAFE_TOTAL
)
PARAM_ONECLICK_GROWTH = PARAM_SAFE_TOTAL - PARAM_ONECLICK_BASE


@dataclass(frozen=True)
class ResourceSpec:
    key: str
    label: str
    input_max: int


RESOURCE_SAFE_MAX = 2_000_000_000

# 金钱输入上限留 1 亿余量：游戏收入结算会把金钱推过 20 亿，之后拒绝保存存档。
RESOURCES = (
    ResourceSpec("money", "金钱", 1_900_000_000),
    ResourceSpec("food", "食物/资源", RESOURCE_SAFE_MAX),
    ResourceSpec("medals", "奖牌", RESOURCE_SAFE_MAX),
    ResourceSpec("stamina", "当前体力", RESOURCE_SAFE_MAX),
)

RESOURCE_FIELDS = tuple((spec.key, spec.label) for spec in RESOURCES)
RESOURCE_INPUT_MAX = {spec.key: spec.input_max for spec in RESOURCES}


def limits_payload():
    """返回注入 Frida 脚本的 JSON 兼容配置。"""
    return {
        "resources": RESOURCE_INPUT_MAX,
        "paramNames": PARAM_NAMES,
        "paramSafeTotal": PARAM_SAFE_TOTAL,
        "paramSafeLimited": list(PARAM_SAFE_LIMITED),
        "paramSumLimit": PARAM_SUM_LIMIT,
    }
