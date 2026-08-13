# optype_collector 采集与冲突检测

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 `optype_collector` 从**哪三个来源**采集 OpType、扫描**什么目录**、解析**什么文件**，并能跟踪一条从命令行到 OpType 集合的完整调用链。
- 解释用户输入的 SoC 名称（如 `Ascend910B`）是如何被映射成 OPP 内部配置目录名（如 `ascend910`）的，包括 `ascend950` 与 `ascend910_95` 这对历史别名的兼容逻辑。
- 对照 `detect_conflicts` 的源码，说明自定义算子与内置算子之间、自定义算子两两之间的重名冲突是如何被**分组**并写入 `ConflictReport` 的，以及三种返回码 `0` / `1` / `2` 分别对应什么情况。

## 2. 前置知识

承接 [u1-l2](u1-l2-directory-structure.md)，你已经知道 `optype_collector` 是 asc-tools 四个 Python 工具之一，位于 `utils/` 下，遵循 `__main__.py`（入口）→ `xxx_main.py`（实现）的二层结构。本讲深入它的实现。在 [u1-l1](u1-l1-project-overview.md) 的工具链里，它属于最后一步「交付体检」——算子开发完成后、交付安装前，用它查一遍命名冲突。

先建立几个 CANN 概念，否则后面的目录路径会看不懂：

- **OPP 包**（Operator Package）：CANN 里算子的交付形态。安装 CANN 后，算子信息按目录组织，典型路径形如 `opp/built-in/op_impl/ai_core/tbe/config/<soc>/`，目录下是一堆 JSON 配置文件，每个文件描述若干算子。optype_collector 扫描的就是这些 JSON。
- **OpType（算子类型名）**：算子的全局唯一标识，例如 `Add`、`MatMul`、`Conv2D`。同一台机器上，不同来源（内置 / 各家自定义）的算子一旦同名，运行时会产生调度歧义——这正是本工具要在交付前提前发现的问题。
- **SoC（System on Chip）**：昇腾处理器型号，如 `Ascend910B`、`Ascend910`、`Ascend310P`。算子配置**按 SoC 分目录**存放，因为不同芯片的算子实现不同。
- **三类采集来源**（本讲第一个最小模块的核心）：
  - 内置算子包：`<CANN安装目录>/cann/opp/built-in`，随 CANN 发布。
  - vendors 自定义算子包：`<CANN安装目录>/cann/opp/vendors/<vendor名>/`，已安装到 CANN 的第三方算子。
  - `ASCEND_CUSTOM_OPP_PATH` 环境变量指向的路径：尚未安装、正在开发/交付中的自定义算子包，支持用系统路径分隔符（`:`/`;`）填多个。

本工具依赖两个环境变量：`ASCEND_HOME_PATH`（CANN 安装根，由 `source set_env.sh` 设置，必填）和 `ASCEND_CUSTOM_OPP_PATH`（自定义算子包路径，可选）。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [utils/optype_collector/optype_collector/optype_collector_main.py](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/optype_collector/optype_collector/optype_collector_main.py) | 全部核心实现：数据结构、扫描、SoC 映射、冲突检测、命令行、输出打印，约 850 行的**单文件**工具 |
| [utils/optype_collector/optype_collector/__main__.py](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/optype_collector/optype_collector/__main__.py) | 模块入口，仅 `from optype_collector.optype_collector_main import main` 后 `sys.exit(main())` |
| [utils/optype_collector/optype_collector.sh](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/optype_collector/optype_collector.sh) | shell 包装，执行 `python3 -m optype_collector "$@"` |
| [docs/05_optype_collector.md](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/05_optype_collector.md) | 用户文档：命令格式、返回码、输出说明 |
| [tests/py_ut/testcase/optype_collector/test_optype_collector.py](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/tests/py_ut/testcase/optype_collector/test_optype_collector.py) | 单元测试，用临时目录构造假 OPP 结构来验证扫描与冲突行为 |

一个重要认知：`optype_collector` 是**纯 Python 单文件、零第三方依赖**的工具（仅用标准库 `argparse` / `json` / `os` / `dataclasses` / `pathlib`）。它不调用任何 NPU 或 CANN 运行时，全靠读文件系统 + 解析 JSON 工作。因此单元测试可以用临时目录"伪造"一个 OPP 目录树来跑（见 `test_optype_collector.py` 的 `setUp`），不需要真实 CANN 环境——这一点对本讲的代码实践很关键。

## 4. 核心概念与源码讲解

本讲按三个最小模块拆分：**OpType 来源扫描**（4.1）、**SoC 名称映射**（4.2）、**冲突检测与返回码**（4.3）。三者是 `main()` 里依次串起的三个阶段：先把算子名"采集"上来，再把 SoC 名"翻译"对，最后对结果"查冲突"。

先认识贯穿全程的几个数据结构（全部是 `@dataclass`）：

- [`OpTypeSource`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/optype_collector/optype_collector/optype_collector_main.py#L30-L59)（L30–L59）：**一个采集来源**的完整描述。字段含 `source_type`（builtin/custom）、`root_path`（扫描目录）、`optypes`（算子名集合）、`config_files`（命中的 JSON 列表）、`warnings`/`errors`。它的 `status` 属性按 errors→ERROR、warnings→WARN、否则 OK 的优先级给出状态，是输出表格里 `Status` 列的数据源。
- [`ScanResult`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/optype_collector/optype_collector/optype_collector_main.py#L62-L73)（L62–L73）：**一次完整扫描**的结果，聚合 `builtin_sources` + `custom_sources` 两组 `OpTypeSource`，并记录用户原始 SoC、展开后的 SoC 列表、环境变量值、全局告警/错误。
- [`ConflictGroup`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/optype_collector/optype_collector/optype_collector_main.py#L76-L83)（L76–L83）/ [`ConflictReport`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/optype_collector/optype_collector/optype_collector_main.py#L86-L95)（L86–L95）：冲突检测结果，详见 4.3。
- [`SocNameMap`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/optype_collector/optype_collector/optype_collector_main.py#L98-L104)（L98–L104）：SoC 名称映射表，详见 4.2。

### 4.1 OpType 来源扫描

#### 4.1.1 概念说明

「采集」回答的问题是：给定一个 SoC，机器上**到底有哪些算子名、各来自哪里**？

实现思路是把"来源"抽象成 `OpTypeSource`，每个来源对应文件系统上一个具体目录。采集 = 遍历三类来源对应的所有候选目录，对存在的目录调用 `_scan_config_dir`，递归读取其下全部 `*.json`，再用 `_collect_optypes_from_json` 从 JSON 里抽出算子名。

一个细节：因为 CANN 的算子配置 JSON 有**多种历史格式**（有的是 `{opType: "Add"}`，有的是 `{"Add": {...}}`，有的是 `{ops: [{name: "Add"}]}`），`_collect_optypes_from_json` 内部用一套**递归 + 三条识别规则**来兼容，这是本模块最值得读的代码。

#### 4.1.2 核心流程

`scan_optypes`（L521–L571）是采集的总入口，流程如下：

```
scan_optypes(user_soc, need_builtin, need_custom):
  1. 读环境变量 ASCEND_HOME_PATH / ASCEND_CUSTOM_OPP_PATH
  2. load_soc_name_map(ascend_home_path)   # 见 4.2，建 SoC 映射表
  3. soc_names = expand_soc_aliases(user_soc, soc_name_map)  # 用户 SoC → 内部目录名列表
  4. 前置校验（任一失败则填 errors 并提前返回）:
       - ASCEND_HOME_PATH 未设置
       - ascend_home_path/opp 不存在
       - soc_names 为空（SoC 不支持）
  5. 若 need_builtin: result.builtin_sources = collect_builtin_optypes(...)
     若 need_custom:  result.custom_sources  = collect_custom_optypes(...)
  6. 返回 ScanResult
```

两个 `collect_*` 函数负责把 SoC 映射成候选目录：

- 内置来源 `collect_builtin_optypes`（L401–L408）：基准目录是 `opp/built-in/op_impl/ai_core/tbe/config`，对每个内部 SoC 名既要扫 `config/<soc>`，也要扫 `config` 下其它分包子目录（如 `config/nn/<soc>`、`config/math/<soc>`）里同名的 SoC 目录——见 [_candidate_builtin_dirs](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/optype_collector/optype_collector/optype_collector_main.py#L385-L398)（L385–L398）。
- 自定义来源 `collect_custom_optypes`（L454–L493）：先扫 `opp/vendors/<vendor>/op_impl/.../config/<soc>`（已安装的第三方包，见 [`_vendor_dirs`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/optype_collector/optype_collector/optype_collector_main.py#L411-L428) L411–L428），再扫 `ASCEND_CUSTOM_OPP_PATH` 里每个路径（既支持直接根 `root/op_impl/...`，也支持根下再分 vendor 子目录两种布局，见 [`_custom_opp_dirs`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/optype_collector/optype_collector/optype_collector_main.py#L431-L451) L431–L451）。

最终落到 [_scan_config_dir](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/optype_collector/optype_collector/optype_collector_main.py#L302-L336)（L302–L336）：`rglob("*.json")` 收集所有 JSON，逐个解析，`optypes.update(...)` 汇总到一个集合里。路径不存在/不是目录记 ERROR；无 JSON/无 OpType 记 WARN。

#### 4.1.3 源码精读

先看 `_scan_config_dir` 的主干（L302–L336），它定义了"一个来源"的扫描边界与异常归类：

```python
json_files = sorted(root_path.rglob("*.json"))
source.config_files = json_files
if not json_files:
    source.warnings.append("No JSON config files found: {}".format(root_path))
    return source

for json_file in json_files:
    data = _read_json_file(json_file, source)   # 解析失败记 WARN，不中断
    if data is None:
        continue
    source.optypes.update(_collect_optypes_from_json(data))
```

要点：单文件解析失败（`_read_json_file` 返回 None）只记 warning，**不中断**其它文件——所以一个坏 JSON 不会让整个来源报废（测试 `test_invalid_json_is_reported_after_scan_without_stopping_other_files` 验证此行为）。

JSON 解析的核心是 [_collect_optypes_from_json](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/optype_collector/optype_collector/optype_collector_main.py#L251-L299)（L251–L299），它用一个内嵌 `visit(obj, parent_key)` 递归遍历任意 JSON 结构，按**三条规则**识别算子名：

```python
explicit_optype_keys = {"opType", "op_type", "opTypeName", "op_type_name", "opName", "op_name"}
container_keys = {"ops", "opList", "op_list", "op_info", "opInfo", "binInfo", "bin_info"}

def visit(obj, parent_key=None):
    if isinstance(obj, dict):
        for key, value in obj.items():
            # 规则1：显式 OpType 键 → value 是算子名
            if key in explicit_optype_keys:
                if isinstance(value, str) and value:
                    optypes.add(value)
                continue
            # 规则2：容器键下的 name 字段 → value 是算子名
            if key == "name" and parent_key in container_keys:
                if isinstance(value, str) and value:
                    optypes.add(value)
                continue
            # 规则3：键名本身像算子（值是 dict/list，父级是根或容器，且通过反例过滤）
            key_is_optype = (
                isinstance(key, str) and isinstance(value, (dict, list))
                and (parent_key is None or parent_key in container_keys)
                and _looks_like_op_type(key)
            )
            if key_is_optype:
                optypes.add(key)
                visit(value, key)
            elif isinstance(value, (dict, list)):
                visit(value, key)
    elif isinstance(obj, list):
        for item in obj:
            visit(item, parent_key)
```

规则 3 配合 [_looks_like_op_type](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/optype_collector/optype_collector/optype_collector_main.py#L206-L248)（L206–L248）工作：后者用一张"黑名单"（`input` / `output` / `attr` / `dtype` / `format` / `bininfo` / `supportinfo` …）排除掉显然不是算子的键，并拒绝以 `_` 开头的内部键。这样既能从 `{"GridSample": {...}}` 里识别出 `GridSample`，又不会把 `GridSample` 内部的 `input0.name = "x"` 误当成算子（测试 `test_nested_parameter_names_are_not_collected_as_optypes` 专门验证这点）。

#### 4.1.4 代码实践（源码阅读 + 可复现小实验）

> **实践目标**：跟踪一条完整的采集调用链，并用"伪造 OPP 树"的方式亲手让工具跑出可观察的结果（无需真实 CANN）。

**第 1 步：阅读型——跟踪 `--all` 的调用链。** 在 [optype_collector_main.py](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/optype_collector/optype_collector/optype_collector_main.py) 中按顺序定位：`main`（L816）→ `_list_mode` 把 `--all` 翻成 `"all"`（L808）→ `scan_optypes(need_builtin=True, need_custom=True)`（L839）→ 内部走 `collect_builtin_optypes` 与 `collect_custom_optypes` → 都调用 `_scan_config_dir` → `_collect_optypes_from_json`。把这条链画出来。

**第 2 步：可复现实验——伪造一棵 OPP 树跑采集。** 下面这段是**示例代码**（仿照 `test_optype_collector.py` 的 `setUp` 写法），在仓库根目录执行即可，无需 CANN：

```python
# 示例代码：构造假 OPP 树，调用 main() 观察 OpType 采集结果
import json, tempfile, contextlib, os
from pathlib import Path
from io import StringIO
from unittest.mock import patch
import sys
sys.path.insert(0, "utils/optype_collector")
from optype_collector.optype_collector_main import main

tmp = Path(tempfile.mkdtemp(prefix="opp_demo_"))
home = tmp / "Ascend" / "latest"
base = home / "opp" / "built-in" / "op_impl" / "ai_core" / "tbe" / "config" / "ascend910b"
base.mkdir(parents=True)
(base / "ops.json").write_text(json.dumps({
    "ops": [{"name": "Add"}, {"name": "MatMul"}],
    "Relu": {},                  # 规则3：键名即算子
}))

out = StringIO()
with patch.dict(os.environ, {"ASCEND_HOME_PATH": str(home)}, clear=True):
    with contextlib.redirect_stdout(out):
        rc = main(["ascend910b", "--builtin"])

print("return code:", rc)
print(out.getvalue())
```

**需要观察的现象**：输出含 `[Scan Info]`、`[Sources]`、`[OpType List]` 三段；算子名 `Add`、`MatMul`（规则 2，来自 `ops[].name`）和 `Relu`（规则 3，键名）都应出现；`Status` 为 `OK`。

**预期结果**：返回码 `0`，三个算子名都被列出。**若你的机器装有 CANN**，可改为直接运行 `optype_collector <你的SoC> --all`（待本地验证真实环境输出）。

#### 4.1.5 小练习与答案

**练习 1**：`_collect_optypes_from_json` 里，规则 1 和规则 2 都要求 `isinstance(value, str) and value`，为什么还要判 `and value`？

> **答案**：过滤掉空字符串 `""`。空串既不是合法算子名，也无法作为 JSON key 出现，加它为的是不让空 `opType: ""` 污染结果集合。

**练习 2**：如果一个 JSON 文件损坏（语法错误），采集是否会中断？为什么？

> **答案**：不会中断。`_read_json_file`（L195–L203）捕获异常后返回 `None` 并向 `source.warnings` 追加 `Failed to parse JSON`，`_scan_config_dir` 见到 `None` 直接 `continue` 跳过该文件，继续处理后续文件。

---

### 4.2 SoC 名称映射

#### 4.2.1 概念说明

用户在命令行输入的 SoC 名（公开展示名，如 `Ascend910B`）往往**不等于** OPP 配置目录里的内部名（如 `ascend910`）。两者之间需要一张映射表，工具才能找到正确的目录去扫描。这张表不写死在代码里，而是**运行时从 CANN 的 `platform_config` 配置文件里读出来**。

本模块解决三件事：(1) 从哪读映射、(2) 怎么解析 ini、(3) 一个特殊的历史别名 `ascend950 ↔ ascend910_95`。

#### 4.2.2 核心流程

```
load_soc_name_map(ascend_home_path):           # L144-L165
  在 <home>/*-linux/data/platform_config/*.ini 里读映射
  每个 ini 含 SoC_version(外) / Short_SoC_version(内) 两行
  → 填三张表:
      external_to_short        : 外名 → 内名列表
      external_lower_to_short  : 外名小写 → 内名列表（大小写不敏感查表用）
      short_to_external        : 内名 → 外名列表

expand_soc_aliases(soc_version, soc_name_map):  # L179-L192
  1. 若输入本身已全小写 (== .lower()):
       → 直接当内名，走 _expand_internal_soc_names([输入])
  2. 否则查表: 先 external_to_short[精确], 再 external_lower_to_short[小写]
       → 命中则 _expand_internal_soc_names(内名列表)
  3. 查不到 → 返回 []（即"不支持"）
```

`_expand_internal_soc_names`（L168–L176）里硬编码了一条别名规则：`ascend950 → ["ascend950", "ascend910_95"]`。这是为兼容旧版 OPP 目录名 `ascend910_95` 而保留的——同一次扫描会同时命中新旧两个目录。

#### 4.2.3 源码精读

先看从哪找 platform_config 目录。[_platform_config_dirs](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/optype_collector/optype_collector/optype_collector_main.py#L134-L141)（L134–L141）用 glob `*-linux` 同时兼容 `x86_64-linux` 与 `aarch64-linux` 两种 CPU 架构，而不是写死其一：

```python
def _platform_config_dirs(ascend_home_path):
    candidates = []
    for child in sorted(ascend_home_path.glob("*-linux")):
        platform_config_dir = child / "data" / "platform_config"
        if platform_config_dir.is_dir():
            candidates.append(platform_config_dir)
    return candidates
```

ini 解析在 [_read_platform_config_soc_names](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/optype_collector/optype_collector/optype_collector_main.py#L112-L131)（L112–L131）。它没有用标准库 `configparser`，而是**逐行手写解析**——按 `=` 切键值、`.lower()` 比较 key（于是 `SoC_version` 与 `soc_version` 都能命中），跳过注释与空行。这样做是为了容忍 ini 文件里多种大小写写法。

最关键的是 [expand_soc_aliases](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/optype_collector/optype_collector/optype_collector_main.py#L179-L192)（L179–L192）的分支逻辑，它决定了"输入什么 → 扫哪些目录"：

```python
def expand_soc_aliases(soc_version, soc_name_map=None):
    normalized_soc = soc_version.lower()
    if soc_version == normalized_soc:                 # 输入已全小写
        return _expand_internal_soc_names([normalized_soc])
    if soc_name_map:
        mapped_names = soc_name_map.external_to_short.get(soc_version)        # 精确查
        if not mapped_names:
            mapped_names = soc_name_map.external_lower_to_short.get(normalized_soc)  # 小写兜底
        if mapped_names:
            return _expand_internal_soc_names(mapped_names)
    return []                                          # 查不到 → 不支持
```

这条逻辑有几个测试钉死的边界（见 `test_expand_soc_aliases_*`）：

- `expand_soc_aliases("ascend950")` → `["ascend950", "ascend910_95"]`（小写直走内名 + 别名展开）。
- `expand_soc_aliases("ascend910_95")` → `["ascend910_95"]`（注意：**不**反向展开成 ascend950，避免回环）。
- `expand_soc_aliases("Ascend950")`（无映射表时）→ `[]`，即"不支持"——大写名必须依赖映射表，否则不认。

#### 4.2.4 代码实践（源码阅读型）

> **实践目标**：验证"同一个 vendor 包在新旧两个 SoC 目录下有算子时，会被合并成**一个**来源，而不是被当成冲突"。

**操作步骤**：
1. 读 [scan_optypes](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/optype_collector/optype_collector/optype_collector_main.py#L521-L571)（L521–L571），确认它先 `load_soc_name_map` 再 `expand_soc_aliases`，把结果存进 `ScanResult.soc_names`。
2. 读 [_deduplicate_sources](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/optype_collector/optype_collector/optype_collector_main.py#L366-L382)（L366–L382）与 [_source_merge_key](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/optype_collector/optype_collector/optype_collector_main.py#L339-L341)（L339–L341）。

**需要观察的现象**：`_source_merge_key` 用 `(source_type, vendor_name, root_path.parent)` 做合并键——**故意只取 `root_path.parent`**，这样同一个包下 `config/ascend950/` 与 `config/ascend910_95/` 两个目录（parent 相同）会被合并成一个 `OpTypeSource`，并把两个 SoC 名 `_merge_matched_soc` 拼成 `"ascend950,ascend910_95"`。

**预期结果**：对应测试 `test_alias_soc_same_custom_source_is_merged_without_custom_custom_conflict` 中，输出 `Custom packages : 1`、`Custom vs Custom : 0 group(s)`。也就是说，别名展开产生的"同包双目录"不会自造冲突——这是把别名处理放在 4.2、把冲突判定放在 4.3 还能协同正确的关键。

#### 4.2.5 小练习与答案

**练习 1**：用户输入 `ascend910B`（注意大小写：首段小写、尾段大写），在没有映射表的情况下，`expand_soc_aliases` 返回什么？为什么？

> **答案**：返回 `[]`。因为 `"ascend910B" != "ascend910b"`（不满足 `soc_version == normalized_soc` 的"已全小写"分支），又没有 `soc_name_map` 可查，于是落到最后的 `return []`。这说明：**大写 SoC 名必须有映射表支持**，否则一律视为不支持。

**练习 2**：为什么 `_read_platform_config_soc_names` 用 `.lower()` 比较 key，而不是直接用 `configparser`？

> **答案**：CANN 的 ini 文件里同一含义的字段可能写成 `SoC_version`、`soc_version`、`SOC_VERSION` 等多种大小写。手写逐行解析并对 key 做小写化比较，能容忍这种写法差异；而 `configparser` 默认大小写敏感、还会把 key 转小写，行为不如手写可控。

---

### 4.3 冲突检测与返回码

#### 4.3.1 概念说明

采集拿到"算子名 → 来源"的全景后，本模块回答：**有没有重名，重名在谁和谁之间**？

冲突分两类（与 [docs/05_optype_collector.md:L85-L88](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/05_optype_collector.md#L85-L88) 一致）：

1. **custom vs built-in**：某个自定义算子名与内置算子名重复。
2. **custom vs custom**：两个**不同**自定义包之间出现同名。

冲突结果用两个数据结构表达：[`ConflictGroup`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/optype_collector/optype_collector/optype_collector_main.py#L76-L83)（L76–L83）描述"一对来源 + 它们重复的算子名列表"；[`ConflictReport`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/optype_collector/optype_collector/optype_collector_main.py#L86-L95)（L86–L95）把所有冲突分成 `custom_builtin` 与 `custom_custom` 两个列表，并用 `has_conflicts` 属性给出总判定。

返回码则由 `main()` 根据"扫描有没有报错"和"冲突报告有没有冲突"综合决定。

#### 4.3.2 核心流程

检测核心 [detect_conflicts](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/optype_collector/optype_collector/optype_collector_main.py#L578-L613)（L578–L613）分两段，都用**集合求交**实现：

```
detect_conflicts(builtin_sources, custom_sources) -> ConflictReport:

  # 第一段：custom vs built-in
  builtin_all = 所有内置算子名的并集
  builtin_reference = builtin_sources[0]   # 取第一个内置来源做"代表"用于报告
  for custom in custom_sources:
      conflicts = sorted(custom.optypes ∩ builtin_all)
      if conflicts: report.custom_builtin.append(ConflictGroup(..., custom, builtin_reference, conflicts))

  # 第二段：custom vs custom（两两组合）
  for i, left in enumerate(custom_sources):
      for right in custom_sources[i+1:]:
          if left.root_path == right.root_path: continue   # 同包跳过
          conflicts = sorted(left.optypes ∩ right.optypes)
          if conflicts: report.custom_custom.append(ConflictGroup(..., left, right, conflicts))
```

集合交运算可写作：\(\text{conflicts} = S_A \cap S_B\)，其中 \(S_A\)、\(S_B\) 是两个来源的 OpType 集合。

返回码逻辑（见 `main` L816–L848）：

| 阶段/模式 | 条件 | 返回码 |
|---|---|---|
| `--detect-conflicts` | 扫描有 errors（环境缺失/SoC 不支持） | `2` |
| `--detect-conflicts` | 扫描无 error，但 `report.has_conflicts` 为真 | `1` |
| `--detect-conflicts` | 扫描无 error 且无冲突 | `0` |
| 列表模式（`--builtin`/`--custom`/`--all`） | 扫描有 errors | `2` |
| 列表模式 | 扫描无 error | `0` |
| 无任何参数 | —— | `2`（打印 help） |

这套返回码与 [docs/05_optype_collector.md:L62-L68](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/05_optype_collector.md#L62-L68) 的定义一致：`0` 成功无冲突、`1` 发现冲突、`2` 阻塞类错误。注意 **errors 优先于冲突**——扫描本身失败时直接 `2`，不会因为恰好也算了冲突就返回 `1`（见 L830–L832）。

#### 4.3.3 源码精读

detect_conflicts 第一段（L582–L597）的关键是把所有内置算子先并成一个集合，再让每个自定义包与这个"全局内置集合"求交，而不是逐对比较——这避免了"自定义包多、内置来源也多"时的笛卡尔积爆炸：

```python
builtin_all = set()
for source in builtin_sources:
    builtin_all.update(source.optypes)
builtin_reference = builtin_sources[0] if builtin_sources else None
if builtin_reference:
    for custom in custom_sources:
        conflicts = sorted(custom.optypes.intersection(builtin_all))
        if conflicts:
            report.custom_builtin.append(
                ConflictGroup("Custom package conflicts with built-in OpTypes",
                              custom, builtin_reference, conflicts))
```

第二段（L599–L612）是自定义包两两求交，用 `enumerate` + 切片 `custom_sources[index+1:]` 避免自比和重复：

```python
for index, left in enumerate(custom_sources):
    for right in custom_sources[index + 1:]:
        if left.root_path == right.root_path:
            continue                       # 同一物理包跳过（别名合并后不会进到这里）
        conflicts = sorted(left.optypes.intersection(right.optypes))
        if conflicts:
            report.custom_custom.append(
                ConflictGroup("Custom package conflicts with another custom package",
                              left, right, conflicts))
```

注意 `if left.root_path == right.root_path: continue` 这一行——它和 4.2 的别名合并是**双保险**：即使两个 `OpTypeSource` 的 `root_path` 完全相同（理论上已被 `_deduplicate_sources` 合并），这里也不会误判为冲突。测试 `test_detect_conflicts_ignores_same_root_custom_sources` 直接构造两个同 `root_path` 的 source 来钉这条防线。

`ConflictReport.has_conflicts`（L93–L95）极其简单，却是返回码的开关：

```python
@property
def has_conflicts(self) -> bool:
    return bool(self.custom_builtin or self.custom_custom)
```

最后看 `main` 里 `--detect-conflicts` 分支的返回（L821–L832），它体现了"errors 优先、冲突其次、成功最后"的三级判定：

```python
if args.detect_conflicts:
    result = scan_optypes(args.detect_conflicts, need_builtin=True, need_custom=True)
    ...
    report = detect_conflicts(result.builtin_sources, result.custom_sources)
    _print_conflicts(result, report)
    _print_source_messages(result)
    if result.errors:
        return 2
    return 1 if report.has_conflicts else 0
```

#### 4.3.4 代码实践（可复现实验 + 源码阅读）

> **实践目标**：亲手制造一次"custom vs built-in + custom vs custom"双重冲突，观察 `ConflictReport` 的分组与返回码，再对照源码解释。

**第 1 步：可复现实验——制造冲突。** 下面是**示例代码**，仿照测试 `test_detects_custom_builtin_and_custom_custom_conflicts` 构造三个来源：内置有 `Add`/`MatMul`，vendor_a 有 `Add`/`CustomSame`，vendor_b 有 `CustomSame`/`Other`。

```python
# 示例代码：制造 custom-vs-builtin 与 custom-vs-custom 双重冲突
import json, tempfile, contextlib, os
from pathlib import Path
from io import StringIO
from unittest.mock import patch
import sys
sys.path.insert(0, "utils/optype_collector")
from optype_collector.optype_collector_main import main

tmp = Path(tempfile.mkdtemp(prefix="conflict_demo_"))
home = tmp / "Ascend" / "latest"

def write(path, data):
    path.mkdir(parents=True, exist_ok=True)
    (path / "c.json").write_text(json.dumps(data))

cfg = lambda root, *parts: root.joinpath("op_impl", "ai_core", "tbe", "config", *parts)
write(cfg(home/"opp"/"built-in", "ascend910b"),          {"Add": {}, "MatMul": {}})
write(cfg(home/"opp"/"vendors"/"vendor_a", "ascend910b"), {"Add": {}, "CustomSame": {}})

custom_root = tmp / "custom_opp"
write(cfg(custom_root/"vendor_b", "ascend910b"),          {"CustomSame": {}, "Other": {}})

out = StringIO()
env = {"ASCEND_HOME_PATH": str(home), "ASCEND_CUSTOM_OPP_PATH": str(custom_root)}
with patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(out):
    rc = main(["--detect-conflicts", "ascend910b"])

print("return code:", rc)          # 预期 1
print(out.getvalue())
```

**需要观察的现象**：
- 返回码 `1`。
- 输出含 `[Conflict Summary]`，`Custom vs Built-in : 1 group(s)`、`Custom vs Custom : 1 group(s)`。
- `[Conflict 1]` 是 custom-vs-builtin，冲突算子 `Add`（vendor_a 与内置重复）。
- `[Conflict 2]` 是 custom-vs-custom，冲突算子 `CustomSame`（vendor_a 与 vendor_b 重复）。
- 每个分组用 A/B 两行表格列出冲突双方的 `Type`/`Vendor`/`Path`。

**预期结果**：返回码 `1`，两类冲突各一组，与上面分析一致。

**第 2 步：源码阅读——解释分组如何影响返回值。** 对照 [detect_conflicts](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/optype_collector/optype_collector/optype_collector_main.py#L578-L613)（L578–L613）回答：vendor_a 的 `Add` 只进入 `report.custom_builtin`（与 `builtin_all` 求交命中），`CustomSame` 只进入 `report.custom_custom`（与 vendor_b 求交命中）；两个列表任一非空，`has_conflicts` 即为 `True`，`main` 据此返回 `1`。

**若你的机器装有 CANN**，可直接运行 `optype_collector <你的SoC> --all` 与 `optype_collector --detect-conflicts <你的SoC>`，对照真实输出验证（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 detect_conflicts 第一段要把所有内置算子先并成 `builtin_all` 再求交，而不是让每个 custom 源逐个与每个 builtin 源比较？

> **答案**：性能。内置算子可能分布在多个来源（整体包 + 各分包），但冲突判定只关心"算子名是否出现在内置集合里"，与具体来自哪个内置来源无关。先并集再求交，复杂度从 \(O(C \times B)\) 降到 \(O(C + B)\)（C、B 为自定义、内置来源数）。报告里的 `builtin_reference` 仅取第一个内置来源作展示用，不参与判定。

**练习 2**：扫描阶段 `ASCEND_HOME_PATH` 没设置，`--detect-conflicts` 返回什么码？为什么不是 1？

> **答案**：返回 `2`。因为 `scan_optypes` 把环境缺失记进 `result.errors`，`main` 里 `if result.errors: return 2` 先于冲突判定执行（L830–L832）。阻塞类错误（码 2）优先于冲突结果（码 1）。测试 `test_detect_conflicts_returns_two_when_scan_has_errors` 验证此点。

**练习 3**：两个自定义包 `root_path` 相同时，detect_conflicts 会报 custom-custom 冲突吗？

> **答案**：不会。第二段循环里有 `if left.root_path == right.root_path: continue` 显式跳过同包。更根本地，别名展开产生的同包多目录已在采集阶段被 `_deduplicate_sources` 按 `root_path.parent` 合并成一个 source，根本不会成为两个独立 source 进入两两比较。这是双重保险。

---

## 5. 综合实践

**任务**：模拟一次完整的算子交付前体检——发现并定位一处命名冲突，全程不依赖真实 CANN。

1. **构造场景**：用 4.3.4 的示例代码思路，伪造一棵 OPP 树，让内置算子含 `Add`、`MatMul`，vendor_a（自定义包 A）也实现了一个 `Add`（想覆盖内置），vendor_b（自定义包 B）实现了一个 `MatMul`（与内置重名）且还实现了 `Add`（与 vendor A 重名）。
2. **运行**：`main(["--detect-conflicts", "ascend910b"])`，收集返回码与输出。
3. **读输出定位**：根据 `[Conflict N]` 段的 A/B 表格，指出每个冲突算子（`Add`、`MatMul`）分别牵涉哪两个来源、属于 custom-vs-builtin 还是 custom-vs-custom。
4. **回到源码验证**：对照 [detect_conflicts](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/optype_collector/optype_collector/optype_collector_main.py#L578-L613) 与 [_print_conflicts](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/optype_collector/optype_collector/optype_collector_main.py#L739-L777)（L739–L777），解释输出里的 group 数是怎么算出来的、`Conflict count` 为何可能是 1 也可能更大。
5. **进阶**：把 vendor_a 的 `Add` 改名成 `MyAdd`，重新运行，确认 custom-vs-builtin 组消失、返回码随之从 `1` 变 `0`（或仍因 custom-custom 保留为 `1`，视你构造的重名而定）。

这个任务串起了采集（4.1）→ SoC 映射（4.2，`ascend910b` 小写直走内名）→ 冲突检测与返回码（4.3）三个模块，是本讲知识的综合应用。

## 6. 本讲小结

- `optype_collector` 是纯 Python 单文件、零第三方依赖的"交付体检"工具，从**三类来源**（`built-in` / `vendors` / `ASCEND_CUSTOM_OPP_PATH`）采集 OpType，扫描的是各来源下 `op_impl/ai_core/tbe/config/<soc>/*.json`。
- 采集核心 `_collect_optypes_from_json` 用**三条识别规则**（显式 OpType 键、容器键下的 `name`、键名本身）+ 黑名单过滤，兼容 CANN 多种历史 JSON 格式；单文件解析失败不中断整体扫描。
- SoC 映射分两层：运行时从 `platform_config/*.ini` 读外名↔内名表（`expand_soc_aliases`），加上硬编码别名 `ascend950 ↔ ascend910_95`；输入大写名必须命中映射表才被支持，否则返回空列表。
- 别名展开产生的"同包多目录"在采集阶段就被 `_deduplicate_sources` 按 `root_path.parent` 合并成一个来源，从根源上避免别名自造冲突——`root_path` 相等跳过是第二道保险。
- 冲突检测 `detect_conflicts` 用集合求交分两类：custom-vs-builtin（自定义包对"内置并集"求交）、custom-vs-custom（自定义包两两求交），结果归入 `ConflictReport`，`has_conflicts` 决定返回码。
- 返回码三级判定：扫描 errors → `2`（阻塞类）；无 error 但有冲突 → `1`；无 error 无冲突 → `0`。errors 优先于冲突。

## 7. 下一步学习建议

- **横向对比其它 Python 工具**：本讲看到 `optype_collector` 沿用了 asc-tools Python 工具的通用范式（`__main__.py` → `xxx_main.py`、单文件实现、`argparse` 命令行）。建议接着读 [u6-l2](u6-l2-objdump-pipeline.md)（msobjdump 的 ObjDump 流程）和 [u7-l3](u7-l3-printf-tensor-timestamp.md)（show_kernel_debug_data 的解析实现），对比三者"薄封装"风格的异同。
- **了解打包与测试**：本工具通过 `setup.py` 的 `console_scripts` 注册命令、`CMakeLists.txt` 打 wheel 包。可预习 [u9-l2](u9-l2-package-install.md)（打包安装）与 [u9-l3](u9-l3-unit-testing.md)（单测体系，本讲的 `test_optype_collector.py` 就是 `tests/py_ut` 体系的一员）。
- **读懂算子工程模板**：本工具检测的是已存在的算子包。若想知道这些算子包是怎么生成的，可继续读 [u8-l2](u8-l2-msopgen-templates.md)（msopgen 与算子工程模板）。
- **源码延伸**：想深入可重点重读 `_collect_optypes_from_json` 的递归访问器与 `_deduplicate_sources` 的合并键设计——前者是处理半结构化数据的范本，后者是"用合并键规避伪冲突"的典型手法。
