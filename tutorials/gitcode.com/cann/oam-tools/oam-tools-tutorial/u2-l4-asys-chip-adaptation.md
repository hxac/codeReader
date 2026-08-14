# asys 芯片适配层：supported_chip 与各型号 handler

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 asys 是如何用「CMake 模板 + 目录约定」在构建期生成 `chip_handler.py` 的，以及为什么运行期一份 `get_device()` 就能把不同芯片分发到不同处理器。
2. 读懂 `supported_chip.py` 的芯片识别流程：正则匹配芯片信息字符串，判断某子命令是否支持当前芯片。
3. 对照 `Ascend910BHandler` / `Ascend91093Handler` / `Ascend950Handler`，理解 handler「按需覆写、默认继承」的差异化设计。
4. 掌握新增一款芯片适配需要动的 3 个文件（handler 目录、`.cmake` 注册文件、无需改动模板本身）。

## 2. 前置知识

本讲建立在前几讲的基础之上，先用两段话把关键前置概念补齐。

**DeviceInfo 与芯片信息字符串**。u2-l3 讲过，`common/device.py` 中的 `DeviceInfo` 用 ctypes 直调驱动 so 库查询设备信息。其中 `get_chip_info(device_id)` 返回一个形如 `"Ascend910B3 Ascend910B3 xxx"` 的字符串（芯片类型 + 芯片名 + 版本号，用空格拼接）。本讲的芯片识别，本质就是拿这个字符串去做正则/子串匹配。

**模板方法模式的直觉**。四款芯片（910B、910_93、910_96、950）的大部分设备查询行为是一样的，只有少数接口有差异（比如 950 是「双 component」封装、温度传感器 ID 不同）。asys 没有为每款芯片写一套完整代码，而是让 `DeviceInfo` 提供默认实现，各芯片 handler 继承它并**只覆写有差异的方法**——这就是经典的模板方法模式：基类定骨架，子类做微调。

还有一个不太直观的点：`chip_handler.py` 这个文件**在仓库里不存在**，它是构建期由 CMake 从 `chip_handler.py.in` 模板生成的。理解这一点是理解整个适配层的关键。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/asys/asys.cmake` | 构建期扫描 `common/ascend*` 目录，收集所有芯片注册项，生成 `chip_handler.py` |
| `src/asys/common/chip_handler.py.in` | `chip_handler.py` 的模板，含 `@ASYS_CHIP_HANDLER_IMPORT_STR@` 和 `@ASYS_CHIP_HANDLER_LIST_STR@` 两个占位符 |
| `src/asys/common/ascend910B/ascend910B_handler.py` | 最小 handler：只覆写 2 个能力开关类方法 |
| `src/asys/common/ascend910B/ascend910B.cmake` | 910B 的注册文件：声明 import 语句和「芯片关键字 → regex → handler」映射 |
| `src/asys/common/ascend910_93/ascend91093_handler.py` | 覆写了 `run_diagnose`，实现多线程并行检测 |
| `src/asys/common/ascend950/ascend950_handler.py` | 差异最大的 handler：适配 950 的双 component 特性 |
| `src/asys/common/supported_chip.py` | 子命令级别的芯片白名单校验 |
| `src/asys/common/__init__.py` | 统一导出 `ChipHandler`、`get_device` 与三个 `*SupportedChip` 类 |
| `src/asys/common/device.py` | `DeviceInfo` 基类（u2-l3 已精读，本讲只引用其 `get_chip_info`） |

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：

- 4.1 适配层全景：构建期注册 + 运行期分发
- 4.2 chip_handler 模板：asys.cmake 如何生成 chip_handler.py
- 4.3 各芯片 handler：从最薄到最厚
- 4.4 supported_chip.py：子命令级芯片白名单

### 4.1 适配层全景：构建期注册 + 运行期分发

#### 4.1.1 概念说明

asys 需要同时支持 ascend910B、ascend910_93、ascend910_96、ascend950 四款芯片，而安装包只装到一个具体环境里。如果四款芯片的代码全部无条件 import，既浪费也会让"新增芯片要改一处中心文件"变成硬耦合。

asys 的解法是把适配层拆成**两个时刻**：

1. **构建期（CMake）**：按目录约定自动发现所有芯片，把每个芯片的 import 语句和注册项拼进模板，生成一份"当前支持全部芯片"的 `chip_handler.py`。
2. **运行期（Python）**：`get_device(device_id)` 查一次芯片信息字符串，按注册表匹配出对应的 handler 实例并缓存；匹配不上就退回通用 `DeviceInfo`。

#### 4.1.2 核心流程

```
构建期：
  asys.cmake
    ├── file(GLOB common/ascend*)               # 发现芯片目录
    ├── include(<芯片目录>/<芯片目录>.cmake)     # 每个芯片的 .cmake 追加两条 list
    │      ASYS_CHIP_HANDLER_IMPORT  += "from ... import XxxHandler"
    │      ASYS_CHIP_HANDLER_LIST    += '"<关键字>": {"regex": ..., "handler": XxxHandler()}'
    └── configure_file(chip_handler.py.in → chip_handler.py, @ONLY)

运行期：
  业务代码 get_device(device_id)
    ├── g_device_map 命中 → 直接返回缓存
    ├── device_id 为 None → 返回通用 DeviceInfo()
    └── 否则 get_chip_info(device_id) 拿字符串
         → ChipHandler().get_handler(chip_info) 按「关键字 in 字符串」匹配
         → 匹配成功返回对应 handler 实例，失败退回 DeviceInfo()
```

#### 4.1.3 源码精读

入口导出在 `common/__init__.py`，业务代码统一从这里拿 `ChipHandler` 和 `get_device`：

[src/asys/common/__init__.py:31-35](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/__init__.py#L31-L35)——第 32 行 `from common.chip_handler import ChipHandler, get_device` 把生成文件里的两个名字提升为包级 API，第 35 行导出三个白名单类。注意第 31 行导出的 `DeviceInfo` 与 `get_device` 并存：前者是"通用设备对象"，后者是"按芯片分发后的设备对象"。

使用方式以 `asys_info.py` 为例：

[src/asys/info/asys_info.py:315](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/info/asys_info.py#L315)——`get_device(device_id).get_device_hbm_info(device_id)`，调用方完全不知道也不关心底层是 `Ascend950Handler` 还是通用 `DeviceInfo`。

#### 4.1.4 代码实践

**实践目标**：确认 `chip_handler.py` 确实是生成文件而非仓库文件。

**操作步骤**：

1. 在仓库根目录执行 `git ls-files | grep chip_handler`。
2. 观察 `git status`，注意 `chip_handler.py` 不会出现在已跟踪文件里。
3. 若本地跑过 `bash build.sh`（u1-l2 讲过），执行 `ls src/asys/common/chip_handler.py` 查看生成产物是否存在，并 `diff src/asys/common/chip_handler.py src/asys/common/chip_handler.py.in` 对比差异。

**需要观察的现象**：`git ls-files` 只列出 `chip_handler.py.in`；生成文件只在构建后出现，且与模板的差异仅在两处 `@...@` 占位符被展开。

**预期结果**：git 只跟踪 `.in` 模板。若从未构建，则 `chip_handler.py` 不存在——此时 `asys` 无法运行，这也解释了为什么 u1-l2 说纯 Python 组件也有"生成 chip_handler.py"这一步。

### 4.2 chip_handler 模板：asys.cmake 如何生成 chip_handler.py

#### 4.2.1 概念说明

模板 `chip_handler.py.in` 定义了适配层的运行期骨架，留了两个占位符：

- `@ASYS_CHIP_HANDLER_IMPORT_STR@`：所有芯片 handler 的 import 语句，按行拼接。
- `@ASYS_CHIP_HANDLER_LIST_STR@`：注册表字典的内容，形如 `"910B" : {"regex": rf"910B\d", "handler": Ascend910BHandler()}, ...`。

`configure_file(... @ONLY)` 会做纯文本替换，"生成"出一份合法 Python 文件。这个设计的妙处：**新增芯片完全不用碰模板和中心代码，只要按目录约定加文件**。

#### 4.2.2 核心流程

`asys.cmake` 的执行过程：

1. `file(GLOB .../common/ascend*)` 拿到所有以 `ascend` 开头的子项，过滤出目录名列表 `FOLDERS_LIST`。
2. 初始化两个空列表 `ASYS_CHIP_HANDLER_LIST`、`ASYS_CHIP_HANDLER_IMPORT`。
3. 逐个 `include(<目录名>/<目录名>.cmake)`——每个芯片的 cmake 文件负责向两个 list 各追加一条字符串。
4. 把 list 按分号转成多行文本（`string(REPLACE ...)`）。
5. `configure_file` 生成 `chip_handler.py`。

注意 `include` 顺序即 glob 顺序，注册表是**按插入顺序遍历**的，这一点在 4.2.3 的 `get_handler` 里会体现出"先到先得"的匹配语义。

#### 4.2.3 源码精读

先看构建脚本如何发现并聚合芯片：

[src/asys/asys.cmake:18-41](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/asys.cmake#L18-L41)——第 18 行 glob 出所有 `ascend*` 子项；第 25-33 行过滤出目录名；第 39-41 行逐个 include 每个芯片目录下**与目录同名**的 `.cmake` 文件。这就是"目录约定"：`common/ascend910B/` 必须提供 `ascend910B.cmake`。

列表转文本并生成文件：

[src/asys/asys.cmake:43-52](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/asys.cmake#L43-L52)——第 43-44 行把 CMake 的分号分隔 list 转成 Python 友好的多行文本（注册表用逗号换行，import 直接换行）；第 48-52 行 `configure_file` 以 `@ONLY` 模式生成 `common/chip_handler.py`。

以 910B 为例，看单芯片的注册内容：

[src/asys/common/ascend910B/ascend910B.cmake:17-18](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/ascend910B/ascend910B.cmake#L17-L18)——第 17 行追加 import 语句；第 18 行追加注册项：字典键 `"910B"` 用于子串匹配，`regex` 为 `rf"910B\d"`（能命中 910B1~910B4 等子型号，与 u1-l1 讲过的关键字匹配规则一致），`handler` 是**在生成文件 import 后立即实例化**的 `Ascend910BHandler()`。

其余三款芯片的注册文件结构完全相同，只是关键字和 regex 不同：

| 注册文件 | 字典键 | regex | handler 类 |
| --- | --- | --- | --- |
| [ascend910B.cmake:18](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/ascend910B/ascend910B.cmake#L18) | `910B` | `rf"910B\d"` | `Ascend910BHandler` |
| [ascend910_93.cmake:17](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/ascend910_93/ascend910_93.cmake#L17) | `910_93` | `"910_93"` | `Ascend91093Handler` |
| [ascend910_96.cmake:17](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/ascend910_96/ascend910_96.cmake#L17) | `910_96` | `"910_96"` | `Ascend91096Handler` |
| [ascend950.cmake:18](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/ascend950/ascend950.cmake#L18) | `950` | `"950"` | `Ascend950Handler` |

再看模板本体（构建后会变成 `chip_handler.py`）：

[src/asys/common/chip_handler.py.in:22-29](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/chip_handler.py.in#L22-L29)——第 22 行 `@ASYS_CHIP_HANDLER_IMPORT_STR@` 展开成 4 行 import；第 27-29 行的字典字面量中 `@ASYS_CHIP_HANDLER_LIST_STR@` 展开成 4 个注册项。私有属性 `__support_chip_with_handler` 就是运行期注册表。

模板中的分发与缓存逻辑：

[src/asys/common/chip_handler.py.in:42-46](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/chip_handler.py.in#L42-L46)——`get_handler(chip_type_str)` 遍历注册表，判断条件是 `support_type in chip_type_str`（**字典键的子串匹配**，不是 regex！）。匹配成功返回对应 handler，全部失败返回通用 `DeviceInfo()`。这是整个适配层的兜底策略：不认识的芯片也能用基类能力跑下去，呼应 u2-l3 讲的"失败退化"气质。

模块级的设备工厂与缓存：

[src/asys/common/chip_handler.py.in:48-60](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/chip_handler.py.in#L48-L60)——`g_device_map` 按 `device_id` 缓存已解析的设备对象（每个设备只查一次芯片信息）；`device_id=None` 直接给通用 `DeviceInfo`；查过一次后同一设备的后续调用全部走缓存。

#### 4.2.4 代码实践

**实践目标**：亲眼看到模板展开后的产物，理解占位符替换。

**操作步骤**：

1. 若已构建：`cat src/asys/common/chip_handler.py | head -40`，找到 4 行 `from common.ascend..._handler import ...` 和字典里的 4 个注册项。
2. 若未构建（推荐，零副作用）：在纸面手动展开——把 4 个 `.cmake` 文件中 `list(APPEND ...)` 的字符串按 glob 顺序填进模板第 22 行和第 28 行，写出你预期生成的 import 区块和字典。
3. 用 `python3 -c "import ast; ast.parse(open('你手写的文件').read())"` 验证语法合法（可选）。

**需要观察的现象**：生成的 import 顺序与 `file(GLOB)` 返回顺序一致；注册表里每个 `handler` 值都是**实例**（`Ascend910BHandler()`）而非类引用。

**预期结果**：手写展开结果与真实生成文件逐字一致（构建过的环境可直接 diff 验证）。若未构建且想确认 glob 顺序，可执行 `ls src/asys/common/ | grep ascend`（文件系统返回顺序即 glob 顺序）。待本地验证：glob 顺序在不同文件系统上可能不同，但因为 4 个键互不为前缀，匹配结果不受顺序影响。

#### 4.2.5 小练习与答案

**练习 1**：`get_handler` 用的是子串匹配 `support_type in chip_type_str`，那注册项里的 `regex` 字段给谁用？

**答案**：给 `supported_chip.py` 用。`ChipHandler().get_support_chip_regex_list()`（模板第 39-40 行）把所有 regex 收集成列表，`CommandSupportedChipBase` 的三个子类用它做白名单校验（见 4.4 节）。也就是说：字典键负责"分发到哪个 handler"（子串匹配），regex 负责"这个芯片算不算被支持"（正则匹配），两者职责分离。

**练习 2**：如果把 `ascend910B.cmake` 里的字典键从 `"910B"` 改成 `"Ascend"`，会发生什么？

**答案**：`get_handler` 的子串匹配会命中所有芯片信息字符串中以 `Ascend` 开头的芯片（`get_chip_info` 返回值形如 `"Ascend910B3 ..."`），导致 910_93/950 等设备也被错误分发到 `Ascend910BHandler`。这提示键必须选到"恰好唯一标识该系列"的粒度。

### 4.3 各芯片 handler：从最薄到最厚

#### 4.3.1 概念说明

四款芯片的 handler 都继承 `DeviceInfo`，差异只体现在覆写了哪些方法。按覆写量从少到多排列，正好是一条"差异化递进"的谱系：

| handler | 覆写内容 | 差异原因 |
| --- | --- | --- |
| `Ascend910BHandler` | 2 个能力开关 | 行为与基类几乎一致，只需声明能力位 |
| `Ascend91093Handler` | 能力开关 + `run_diagnose` | 诊断需要按"逻辑主卡"多线程并行 |
| `Ascend91096Handler` | （与 910_93 类似，本讲不展开） | 同系列 |
| `Ascend950Handler` | 能力开关 + 6 个查询方法 | 950 是双 component 封装，且温度/电压/频率语义不同 |

#### 4.3.2 核心流程

handler 的统一隐式接口（全部继承自 `DeviceInfo`，子类按需覆写）：

```
classmethod:
  need_lp_param()        # 诊断检测是否需要 lp 参数
  support_dvpp()         # 是否支持 dvpp
instance method:
  get_device_* (device_id)   # aic/bus/hbm/voltage/frequency/temperature 等查询
  run_diagnose(device_obj, diagnose_devices, run_mode)  # 诊断执行入口
```

950 的"双 component"是本模块最值得讲的设计：一颗 950 芯片在 DSMI 接口层面表现为两个 component，设备号 `device_id` 编码 component 0，而 `(0x0001 << 16) | (device_id & 0xFFFF)` 编码 component 1。查询信息时两个 component 各查一次再拼成 `"v0, v1"` 形式的字符串返回。

#### 4.3.3 源码精读

**最薄适配：910B**。

[src/asys/common/ascend910B/ascend910B_handler.py:23-37](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/ascend910B/ascend910B_handler.py#L23-L37)——整个类只有 3 个方法：构造函数调 `super().__init__()`；`need_lp_param()` 返回 `False`；`support_dvpp()` 返回 `True`；`run_diagnose` 直接转调 `interface.run_diagnose`（与基类行为一致，显式写出是为了可读性）。所有 `get_device_*` 查询全部继承基类，零覆写。

**中等适配：910_93 的多线程诊断**。

[src/asys/common/ascend910_93/ascend91093_handler.py:36-66](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/ascend910_93/ascend91093_handler.py#L36-L66)——`run_diagnose` 被覆写为多线程版本：先查每个设备的逻辑主卡（`get_devices_master_id`）；对每种检测模式（`hbm_detect`/`cpu_detect`）选取对应执行函数；然后**每个逻辑主卡只开一个线程**去检测，从设备直接复用主设备的检测结果。这样避免了同一路径上的多张卡重复检测。注意第 48 行的兜底：非 hbm/cpu 检测模式仍走通用 `interface.run_diagnose`。

**最厚适配：950 的双 component**。

[src/asys/common/ascend950/ascend950_handler.py:28-53](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/ascend950/ascend950_handler.py#L28-L53)——类头定义了编码常量：`SHIFT_BIT = 16`、`MASK_BIT = 0xFFFF`、`OFFSET = 0x0001`。`get_encode_component_one_id` 把设备号编码成 component 1 的 ID：高 16 位移位检查（设备号必须小于 65536，否则记 debug 日志并返回 0 表示不支持），然后 `(OFFSET << 16) | (device_id & 0xFFFF)`。`need_lp_param` 对 950 返回 `True`（与 910B 相反）。

[src/asys/common/ascend950/ascend950_handler.py:55-72](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/ascend950/ascend950_handler.py#L55-L72)——`get_device_aic_info` 和 `get_device_bus_info` 是双 component 适配的典型写法：先 `super().get_device_xxx(device_id)` 查 component 0，再用编码后的 `component_one_id` 查 component 1，最后把两组值拼成 `"v0, v1"` 字符串。component 1 查询失败时以 `NOT_SUPPORT` 占位（再次呼应 u2-l3 的失败占位哲学）。

[src/asys/common/ascend950/ascend950_handler.py:92-110](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/ascend950/ascend950_handler.py#L92-L110)——两处"语义微调"：`get_device_voltage` 在基类返回值后追加 `(Max)` 标注（950 上报的是最大电压而非当前电压）；`get_device_temperature` 则完全重写，改用 `dsmi_get_soc_sensor_info` + `SOC_TEMP_ID` 查 SoC 温度，而不是基类的温度传感器路径。

#### 4.3.4 代码实践

**实践目标**：亲手推导 950 component 编码，验证对 `get_encode_component_one_id` 的理解。

**操作步骤**：

1. 写一个 15 行以内的独立 Python 片段（示例代码，不属于项目）：

```python
# 示例代码：复现 Ascend950Handler.get_encode_component_one_id 的编码逻辑
SHIFT_BIT, MASK_BIT, OFFSET = 16, 0xFFFF, 0x0001

def encode(device_id):
    if device_id >> SHIFT_BIT:
        return 0
    return (OFFSET << SHIFT_BIT) | (device_id & MASK_BIT)

for d in (0, 1, 65535, 65536, 70000):
    print(d, "->", encode(d))
```

2. 运行并记录输出。
3. 对照源码 [ascend950_handler.py:38-45](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/ascend950/ascend950_handler.py#L38-L45) 确认逻辑一致。

**需要观察的现象**：`0→65536(0x10000)`、`1→0x10001`、`65535→0x1FFFF`、`65536→0`、`70000→0`。

**预期结果**：设备号合法区间 `[0, 65536)` 内，component 1 的 ID 恰好等于 `device_id + 0x10000`；越界时返回 0，后续代码用 `if component_one_id:` 判空跳过 component 1 查询。本实践可本地验证（纯位运算，无设备依赖）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Ascend910BHandler.run_diagnose` 只是转调 `interface.run_diagnose`，几乎等于不写？删掉它可以吗？

**答案**：功能上可以删（继承后行为相同），但显式写出让每款芯片的诊断入口在各自 handler 里可见，新增芯片的人能一眼看到"这里有一个可覆写的差异点"。这是可读性取舍，不是功能必需。

**练习 2**：`Ascend950Handler.get_device_voltage` 里 `voltage_value != NOT_SUPPORT` 的判断防的是什么？

**答案**：基类查询失败时返回占位值 `NOT_SUPPORT`（驱动 so 缺失或调用失败，见 u2-l3 的三重防御）。如果不判断，输出会变成 `"NOT_SUPPORT(Max)"` 这种畸形式样；判断后保持干净的 `NOT_SUPPORT`。

### 4.4 supported_chip.py：子命令级芯片白名单

#### 4.4.1 概念说明

4.2 节解决的是"这个设备用哪个 handler 查信息"，本节解决另一个问题：**某个子命令（config/diagnose/profiling）在当前芯片上到底能不能跑**。例如某款新芯片已能查询基本信息（走 `DeviceInfo` 兜底），但诊断检测尚未适配，此时应在入口处明确拒绝而不是跑出错乱结果。

实现上是一个基类 + 三个子类。基类 `CommandSupportedChipBase` 持有 `SUPPORTED_CHIP_TYPE` 正则列表，用 `get_supported_chip_info` 判断当前设备芯片是否命中任意一条正则。三个子类 `AsysConfigSupportedChip`、`AsysDiagnoseSupportedChip`、`AsysProfilingSupportedChip` 目前都取自 `ChipHandler().get_support_chip_regex_list()`——即"handler 注册表里有的芯片就支持"。

#### 4.4.2 核心流程

```
业务子命令 (config / diagnose / profiling)
  └── XxxSupportedChip().get_supported_chip_info(device_id)
        ├── DeviceInfo().get_chip_info(device_id)   # 拿芯片信息字符串
        ├── any(re.search(rf"{i}", chip_info) for i in SUPPORTED_CHIP_TYPE)
        └── 返回 (True/False, chip_info)
```

为什么要拆成三个类而不是共用一个？因为**不同子命令的支持范围天然可能不同**——现在三者恰好一致，但结构上预留了各自独立配置 `SUPPORTED_CHIP_TYPE` 的能力。

#### 4.4.3 源码精读

基类的判断逻辑：

[src/asys/common/supported_chip.py:23-35](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/supported_chip.py#L23-L35)——`get_supported_chip_info(device_id)` 先通过 `self.device.get_chip_info(device_id)` 拿到芯片信息字符串，再用生成器 `any(re.search(rf"{i}", chip_info) for i in self.SUPPORTED_CHIP_TYPE)` 逐条正则匹配，任意命中即支持。返回值带上 `chip_info` 本身，方便调用方在不支持的提示里直接打印芯片名。

三个子命令白名单类：

[src/asys/common/supported_chip.py:38-53](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/supported_chip.py#L38-L53)——三个类的 `SUPPORTED_CHIP_TYPE` 都来自 `ChipHandler().get_support_chip_regex_list()`，也就是 [chip_handler.py.in:39-40](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/chip_handler.py.in#L39-L40) 收集的 `["910B\d", "910_93", "910_96", "950"]`（构建后实际值）。注意类属性在**类定义时求值**，因此 import `supported_chip` 之前 `chip_handler.py` 必须已生成。

实际消费方以 diagnose 和 config 为例：

[src/asys/diagnose/asys_diagnose.py:187](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/diagnose/asys_diagnose.py#L187)——diagnose 子命令实例化 `AsysDiagnoseSupportedChip` 做前置芯片校验。

[src/asys/config_cmd/asys_config.py:55](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/config_cmd/asys_config.py#L55)——config 子命令用 `AsysConfigSupportedChip().get_supported_chip_info` 判断后决定是否继续。

#### 4.4.4 代码实践

**实践目标**：验证白名单匹配逻辑对芯片信息字符串的行为。

**操作步骤**：

1. 写一个 20 行以内的独立片段（示例代码，脱离设备模拟匹配过程）：

```python
# 示例代码：模拟 CommandSupportedChipBase.get_supported_chip_info
import re

SUPPORTED_CHIP_TYPE = [r"910B\d", "910_93", "910_96", "950"]

def get_supported_chip_info(chip_info):
    if any(re.search(rf"{i}", chip_info) for i in SUPPORTED_CHIP_TYPE):
        return True, chip_info
    return False, chip_info

for ci in ("Ascend910B3 Ascend910B3 V01", "Ascend910_93 V02", "Ascend950 xxx",
           "Ascend310P3 xxx", "unknown"):
    print(ci, "->", get_supported_chip_info(ci))
```

2. 运行并记录每条的返回值。
3. 对照 [supported_chip.py:31-35](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/supported_chip.py#L31-L35) 确认复现一致。

**需要观察的现象**：前三条返回 `(True, ...)`；`Ascend310P3` 与 `unknown` 返回 `(False, ...)`。

**预期结果**：`910B\d` 正则要求 910B 后必须跟数字，所以裸 `"910B"` 结尾的字符串不命中；310P 系列未被适配，走白名单拒绝路径。本实践可本地验证（纯字符串处理）。

#### 4.4.5 小练习与答案

**练习 1**：`get_supported_chip_info` 返回的 `chip_info` 有什么用途？为什么不只返回 bool？

**答案**：调用方在芯片不受支持时要给用户打印"当前芯片是 xxx，不被支持"的提示，`chip_info` 直接复用查询结果，避免二次查询设备。

**练习 2**：如果未来某子命令只支持 950 一款芯片，改动最小的方式是什么？

**答案**：把对应子类（如 `AsysProfilingSupportedChip`）的 `SUPPORTED_CHIP_TYPE` 从 `ChipHandler().get_support_chip_regex_list()` 改为硬编码 `["950"]` 即可——这正是拆成三个子类预留的扩展点，无需动基类和其他子类。

## 5. 综合实践

**任务：为假想芯片 ascendXXX 完成一套纸面适配方案**（即任务规格中的实践任务，不需要真实硬件）。

1. **写 handler 骨架**。新建 `src/asys/common/ascendXXX/ascendXXX_handler.py`（以下为示例代码，按 `ascend910B_handler.py` 的结构照抄签名）：

```python
# 示例代码：假想芯片 handler 骨架
from common.device import DeviceInfo
import common.interface as interface


class AscendXXXHandler(DeviceInfo):
    def __init__(self):
        super().__init__()

    @classmethod
    def need_lp_param(cls):
        return False  # 按假想芯片的检测要求定

    @classmethod
    def support_dvpp(cls):
        return True

    # 有差异的查询才覆写，例如：
    # def get_device_temperature(self, device_id):
    #     ...自定义实现...

    def run_diagnose(self, device_obj, diagnose_devices, run_mode):
        return interface.run_diagnose(device_obj, diagnose_devices, run_mode)
```

2. **写注册文件**。新建 `src/asys/common/ascendXXX/ascendXXX.cmake`（示例代码）：

```cmake
# 示例代码：注册 ascendXXX
list(APPEND ASYS_CHIP_HANDLER_IMPORT "from common.ascendXXX.ascendXXX_handler import AscendXXXHandler")
list(APPEND ASYS_CHIP_HANDLER_LIST "\"XXX\" : {\"regex\": \"XXX\", \"handler\": AscendXXXHandler()}")
```

3. **确认接入点**。芯片注册点是 [asys.cmake:18](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/asys.cmake#L18) 的 `file(GLOB .../common/ascend*)`——目录名以 `ascend` 开头即被自动发现，第 40 行会自动 include 与目录同名的 `.cmake`。**不需要改 `chip_handler.py.in`、`supported_chip.py` 或任何中心文件**；白名单因取自 `get_support_chip_regex_list()` 自动包含新芯片。

4. **验证**（待本地验证，需构建环境）：`python3 -m pytest test/ut/asys -k chip` 或重新执行 `cmake` 配置后检查生成的 `chip_handler.py` 是否多出 `ascendXXX` 的 import 与注册项。

## 6. 本讲小结

- asys 芯片适配层分两个时刻：构建期由 `asys.cmake` glob 出 `common/ascend*` 目录、聚合各 `.cmake` 注册项，经 `configure_file` 从 `chip_handler.py.in` 生成 `chip_handler.py`；仓库中不存在生成文件本身。
- 运行期由模块级工厂 `get_device(device_id)` 按 `g_device_map` 缓存分发：芯片信息字符串命中注册表键（子串匹配）返回对应 handler，否则退回通用 `DeviceInfo` 兜底。
- 各芯片 handler 是模板方法模式：`Ascend910BHandler` 只声明 2 个能力开关；`Ascend91093Handler` 覆写 `run_diagnose` 实现按逻辑主卡多线程检测；`Ascend950Handler` 最厚，用 `(0x0001 << 16) | device_id` 编码双 component 并覆写 6 个查询方法。
- 注册项中字典键负责子串分发，`regex` 负责白名单正则，两者职责分离；`supported_chip.py` 的三个子命令白名单类统一取自 `get_support_chip_regex_list()`，预留了按子命令独立配置的扩展点。
- 新增一款芯片只需 2 个新文件（handler + 与目录同名的 `.cmake`），中心代码零改动。

## 7. 下一步学习建议

- 下一讲 u2-l5 将进入 `src/asys/collect` 采集子系统：`asys_collect.py` 如何调度各采集项，而本讲的 handler/`DeviceInfo` 正是采集项获取设备信息的底层依赖。
- 想加深对 handler 差异化理解，可通读 [ascend910_96/ascend91096_handler.py](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/ascend910_96/ascend91096_handler.py)，比较它与 910_93 handler 的异同。
- 建议顺带阅读 [test/ut/asys](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/test/ut/asys) 下与芯片/设备相关的测试，看测试如何 mock 芯片信息字符串（u6-l1 会系统讲测试体系）。
