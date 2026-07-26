# 实验内核追踪与内核清单生成

## 1. 本讲目标

本讲聚焦 TileGym 的两条「工程治理」主线，它们都不改变内核的计算结果，而是回答两个管理问题：

1. **哪些内核还不成熟？** 当一个内核来自外部贡献、尚未经核心团队完整验证时，如何在用户首次启动它时给出一次性告警——而且**不修改内核函数体本身**？
2. **仓库里到底有哪些内核、它们的输入输出与参考实现是什么？** 如何把散落在各处的内核元数据组织成一份机器可读、可校验的「清单（inventory）」，并让它与 FlashInfer-Bench（FIB）的 Definition/Solution 数据模型兼容？

学完本讲，你应当能够：

- 解释 `experimental_kernel` 装饰器 + `ct.launch` monkey-patch 如何实现「启动时一次性告警」，并说出两层去重机制。
- 说出 kernel_inventory 的 Definition / Solution / kernels 三类文件各自的作用与磁盘布局。
- 列出 `generation.py` 中的核心 pydantic 模型（`TileGymSourceFile` / `TileGymBuildSpec` / `TileGymSolution`）及其校验规则。
- 解释 `iter_kernel_solution_paths` 现在如何遍历 `triton` / `cutile` / `cutile_rs` 三类后端目录。
- 理解 `TILEGYM_DISABLE_AUTOTUNE` 全局开关的取值语义与适用范围。

## 2. 前置知识

在进入源码前，先建立几个直觉概念：

- **monkey-patch（猴子补丁）**：在运行时把某个模块的函数替换成自己的版本，从而改变其行为，但不改动它的源文件。本讲里被替换的是 `cuda.tile.launch`。
- **装饰器（decorator）**：形如 `@something` 的语法，本质是「接收一个函数、返回一个新函数」的高阶函数。本讲里 `@experimental_kernel` 装饰的是一个**已经被 `@ct.kernel` 处理过的内核对象**。
- **pydantic 模型**：Python 的数据校验库，用类来声明字段类型，构造时自动校验、出错抛 `ValidationError`。可看成「带类型检查的 dataclass」。
- **Definition / Solution**：这是 FlashInfer-Bench（FIB）提出的两类元数据——**Definition** 描述「一个算子应该做什么」（输入/输出张量形状、轴、参考实现），**Solution** 描述「某后端如何实现它」（源码路径、入口函数、目标硬件）。一对 Definition/Solution 类似「接口 / 实现」的关系。
- **autotune（自动调优）**：在多个候选配置（瓦片大小、occupancy 等）中实测择优。本讲不深入算法（那是 u5-l3 的内容），只讲它的**全局开关**。

> 本讲承接 u3-l3（`ct.launch` 四参调用约定）与 u2-l2（注册表分发）。如果你还不熟悉 `ct.launch(stream, grid, kernel, kernel_args)` 的参数顺序，建议先回顾 u3-l3。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/tilegym/experimental.py](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/experimental.py) | `experimental_kernel` 装饰器 + 对 `ct.launch` 的 monkey-patch，负责实验内核的一次性告警。 |
| [src/tilegym/__init__.py](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/__init__.py) | 包入口，在 `import tilegym` 时按条件调用 `_apply_patch()` 安装补丁。 |
| [src/tilegym/logger.py](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/logger.py) | 提供 `warn_once` 全局去重告警，是补丁的第二层去重依赖。 |
| [src/tilegym/kernel_inventory/\_\_init\_\_.py](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/kernel_inventory/__init__.py) | 清单遍历与校验入口：`iter_kernel_*_paths`、`validate_definition` / `validate_solution`。 |
| [src/tilegym/kernel_inventory/generation.py](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/kernel_inventory/generation.py) | FIB 形状的 pydantic 模型与构造/校验函数（懒加载，避免运行时强依赖 flashinfer-bench）。 |
| [src/tilegym/kernel_inventory/source_contract.py](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/kernel_inventory/source_contract.py) | 校验 Definition.reference 必须以精确的 GitHub/HF 永久链接开头。 |
| [src/tilegym/autotune.py](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/autotune.py) | 全局自动调优开关：`TILEGYM_DISABLE_AUTOTUNE` 环境变量与两个查询函数。 |
| [tests/kernel_inventory/test_kernel_inventory.py](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/tests/kernel_inventory/test_kernel_inventory.py) | 清单的权威用法范例：遍历、校验、目录完整性断言。 |

## 4. 核心概念与源码讲解

### 4.1 实验内核告警：experimental_kernel 装饰器与 launch 补丁

#### 4.1.1 概念说明

TileGym 欢迎外部贡献内核，但贡献的内核未必经过核心团队完整验证。如果用户不知不觉地用了一个「实验性」内核跑生产任务，风险很高。于是需要一个机制：

- **标记**：让作者在内核上挂一个「我是实验性的」标签。
- **告警**：在用户**真正启动**这个内核时，打印一条警告，而且只打印一次，避免刷屏。
- **零侵入**：不能要求作者在内核函数体里写 `print` 或改算法逻辑。

TileGym 的解法是把「标记」与「告警」解耦：装饰器只负责挂标签，真正的告警由一个**全局的 `ct.launch` 补丁**在启动边界统一完成。这样内核函数体一行都不用改。

#### 4.1.2 核心流程

```text
@experimental_kernel        # 1. 标记层：给内核对象挂上 _tracked_message 属性
@ct.kernel                  # 2. 先被 ct.kernel 装饰成内核对象
def my_kernel(...): ...      #    函数体完全不知情

        │
        ▼  （用户调用 ct.launch 时）

import tilegym               # 3. import 时 _apply_patch() 把 ct.launch 换成 _patched_launch
        │
        ▼

_patched_launch(stream, grid, kernel, args)
    ├── 读 kernel._tracked_message
    ├── 若非空 → warn_once(msg, "EXPERIMENTAL")  ← 第二层去重
    ├── kernel._tracked_message = None            ← 第一层去重（清自身）
    └── 调原始 _original_launch 真正启动
```

关键点：装饰器的顺序是 `@experimental_kernel` 在**外**、`@ct.kernel` 在**内**。这样 `experimental_kernel` 拿到的是已经被 `ct.kernel` 包装好的内核对象，可以直接往它身上塞属性。

#### 4.1.3 源码精读

先看标记层。装饰器要兼容三种写法：裸用 `@experimental_kernel`、带空括号 `@experimental_kernel()`、带自定义消息 `@experimental_kernel("msg")`。

[experimental.py:25-65 — experimental_kernel 装饰器](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/experimental.py#L25-L65) 通过判断参数类型区分三种用法，最终都归结为给内核对象设置 `_tracked_message` 属性：

```python
def decorator(kernel_obj):
    msg = message if message is not None else _default_message(kernel_obj)
    kernel_obj._tracked_message = msg   # 仅挂属性，不碰函数体
    return kernel_obj
```

其中 `_default_message` 会尽力从内核对象上取名字（`__name__` → `_pyfunc.__name__` → `name`），拼出默认告警文案（[experimental.py:12-22](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/experimental.py#L12-L22)）。

再看告警层。模块加载时保存原始函数引用：

[experimental.py:5-9 — 保存原始 launch](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/experimental.py#L5-L9)

```python
import cuda.tile as ct
_original_launch = ct.launch        # 先存好「真品」
```

[experimental.py:68-73 — _patched_launch 补丁](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/experimental.py#L68-L73) 在每次启动时检查属性、告警、清空、再转发：

```python
def _patched_launch(stream, grid, kernel, kernel_args, /):
    msg = getattr(kernel, "_tracked_message", None)
    if msg:
        warn_once(msg, "EXPERIMENTAL")     # 全局去重
        kernel._tracked_message = None     # 自身去重
    return _original_launch(stream, grid, kernel, kernel_args)
```

注意签名里的 `/` 表示前四个参数只能按位置传，这正好对应 u3-l3 讲过的 `ct.launch(stream, grid, kernel, kernel_args)` 四参约定。

补丁的安装发生在 `import tilegym` 时，且**只在 cuTile 后端可用时**才装（因为 `ct.launch` 来自 `cuda.tile`）：

[\_\_init\_\_.py:42-46 — 条件安装补丁](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/__init__.py#L42-L46)

```python
if is_backend_available("cutile"):
    from .experimental import _apply_patch as _apply_experimental_patch
    _apply_experimental_patch()
```

而 `_apply_patch` 本体只有一行——把 `ct.launch` 指向补丁（[experimental.py:76-78](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/experimental.py#L76-L78)）：`ct.launch = _patched_launch`。

**两层去重**是本模块最精妙之处：

| 层次 | 位置 | 机制 | 作用范围 |
| --- | --- | --- | --- |
| 第一层 | `_patched_launch` | 打印后把 `kernel._tracked_message` 置 `None` | 单个内核对象：同一编译产物的重复启动不再告警 |
| 第二层 | `warn_once(msg, "EXPERIMENTAL")` | 以 `category:message` 为 key 去重 | 进程级：不同内核对象但文案相同也只告警一次 |

第二层依赖 `warn_once`，它内部用一个 `threading.Lock` 保护的集合做去重（[logger.py:132-150](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/logger.py#L132-L150)）：

```python
def warn_once(self, message, category=None, ...):
    key = f"{category}:{message}" if category else message
    with self._lock:
        if key not in self._warned_messages:
            self._warned_messages.add(key)
            ... self.logger.warning(formatted_message, ...)
```

> **诚实说明**：文件末尾的 `reset_tracking()`（[experimental.py:81-84](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/experimental.py#L81-L84)）目前函数体是 `pass`——一个空占位实现。它的文档字符串声称能「重置所有实验内核消息以便重新打印（用于测试）」，但实际上它什么也没做。真正能重置的是 logger 侧的 `reset_warning_cache()`。这是因为消息已被「清空」在散落各处的内核对象属性上，没有中心化登记表，难以逆向找回。

#### 4.1.4 代码实践

**实践目标**：验证「告警只在首次启动时出现」。

**操作步骤**（源码阅读型）：

1. 打开 [src/tilegym/ops/cutile/softmax.py:118](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/softmax.py#L118)，确认 chunked softmax 内核上方挂着 `@experimental_kernel`（在 `@ct.kernel` 之外）。
2. 在仓库根目录运行：

   ```bash
   python -c "
   import tilegym
   from tilegym.ops import softmax
   import torch
   x = torch.randn(256, 8192, device='cuda', dtype=torch.float32)
   softmax(x, use_chunked=True)   # 首次启动实验内核 → 应见一条 [EXPERIMENTAL] 警告
   softmax(x, use_chunked=True)   # 第二次 → 不应再见警告
   "
   ```

**需要观察的现象**：第一次调用打印一条类似 `... is an experimental kernel contributed by external GitHub TileGym contributors. This kernel has not been fully validated by the core team.` 的警告；第二次调用静默。

**预期结果**：两次调用结果一致（数值正确），但警告只出现一次。

> 若当前机器没有可用 GPU 或 cuTile 后端，上述命令无法运行，则改为**源码阅读型实践**：在 `experimental.py` 中标注出「第一层去重」与「第二层去重」分别由哪两行代码完成，并解释若只保留第一层、删掉第二层，在什么场景下会重复告警（提示：cuTile 的 `replace_hints` 会生成新的内核对象）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `@experimental_kernel` 和 `@ct.kernel` 的顺序写反（`@ct.kernel` 在外），会发生什么？

**参考答案**：此时 `experimental_kernel` 拿到的是**原始 Python 函数**而非内核对象，`_tracked_message` 会挂在普通函数上；而真正传给 `ct.launch` 的是 `ct.kernel` 包装后的对象，其上没有该属性，补丁读不到消息，告警失效。所以装饰器顺序不可颠倒。

**练习 2**：为什么补丁要保存 `_original_launch = ct.launch` 而不是直接在 `_patched_launch` 里调 `ct.launch`？

**参考答案**：因为补丁安装后 `ct.launch` 已被替换成 `_patched_launch` 本身。若在补丁里再调 `ct.launch`，会无限递归。保存「真品」引用是 monkey-patch 的标准做法。

### 4.2 kernel_inventory 的数据契约：Definition / Solution / kernels

#### 4.2.1 概念说明

随着内核数量增长，需要一份「清单」回答：仓库里实现了哪些算子？每个算子的输入输出形状、数据类型、参考实现（reference）是什么？某个后端的实现源码在哪？这份清单既给人看（文档），也给机器用（CI 校验、与 FIB 生态对接）。

TileGym 借用 FlashInfer-Bench（FIB）的两类元数据对象来组织：

- **Definition（定义）**：算子的「规格」。声明轴（axes）、输入张量（inputs）、输出张量（outputs）、约束（constraints），以及一段 Python `reference` 参考实现。
- **Solution（解）**：某个后端对某个 Definition 的「实现」。声明源码路径（sources）、入口函数（entry_point）、语言、目标硬件。

两者通过 `definition` 字段关联：一个 Solution 的 `definition` 字段等于某个 Definition 的 `name`。

#### 4.2.2 核心流程：磁盘布局

清单文件按固定目录约定存放。`kernel_inventory/__init__.py` 顶部说明了这一布局（[kernel_inventory/\_\_init\_\_.py:5-17](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/kernel_inventory/__init__.py#L5-L17)）：

```text
src/tilegym/transformers/<model>/
    kernel_definitions/<name>.json     # Definition 对象
    kernel_solutions/<name>.json       # （transformer 模型）单一 Solution，无后端子目录
    kernels/<name>.py                  # 被引用的可复用内核实现

src/tilegym/suites/<suite>/
    kernel_definitions/<name>.json     # 公共 Definition 放根目录
    kernel_solutions/
        triton/<name>.json             # 按「后端」分子目录
        cutile/<name>.json
        cutile_rs/<name>.json
```

也就是说，TileGym 支持两种清单布局：

- **transformer 布局**：每个模型一个目录，Definition 与 Solution 平级，Solution 直接放在 `kernel_solutions/` 根（不带后端子目录）。
- **suite 布局**：Definition 放在 `kernel_definitions/` 根，Solution 按 `triton` / `cutile` / `cutile_rs` 三类后端分子目录存放——这样一个 Definition 可以有多个后端的 Solution。

真实仓库里目前落地的是 transformer 布局（如 `src/tilegym/transformers/qwen3_5/`），suite 布局是**为 suites 扩展预留的能力**（见 4.4 节）。

来看一份真实的 Definition 片段（[transformers/qwen3_5/kernel_definitions/qwen3_5_sigmoid_mul.json](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/transformers/qwen3_5/kernel_definitions/qwen3_5_sigmoid_mul.json)）：

```json
{
  "name": "qwen3_5_sigmoid_mul",
  "op_type": "sigmoid_mul",
  "axes": { "D": {"type": "var"}, "N": {"type": "var"} },
  "inputs":  { "x": {"dtype": "bfloat16", "shape": ["N","D"]},
               "gate": {"dtype": "bfloat16", "shape": ["N","D"]} },
  "outputs": { "output": {"dtype": "bfloat16", "shape": ["N","D"]} },
  "reference": "# Source: https://github.com/huggingface/transformers/blob/.../modeling_qwen3_5.py#L785-L786\nimport torch\n\ndef run(x, gate):\n    return x * torch.sigmoid(gate)"
}
```

对应的 Solution（[transformers/qwen3_5/kernel_solutions/qwen3_5_sigmoid_mul.json](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/transformers/qwen3_5/kernel_solutions/qwen3_5_sigmoid_mul.json)）：

```json
{
  "name": "qwen3_5_sigmoid_mul_cutile",
  "definition": "qwen3_5_sigmoid_mul",
  "spec": {
    "language": "cuda-tile",
    "entry_point": "src/tilegym/transformers/qwen3_5/kernels/sigmoid_mul.py::sigmoid_mul_cutile",
    "target_hardware": ["SM100", "SM103", "SM120"],
    "destination_passing_style": false
  },
  "sources": { "path": ["src/tilegym/transformers/qwen3_5/kernels/sigmoid_mul.py"] }
}
```

注意两个约定：① Solution 文件名等于 `definition` 名；② `sources` 用**仅路径**（`{"path": [...]}`）形式，不内嵌源码内容——这是 TileGym 相对 FIB 原版做的简化。

#### 4.2.3 源码精读：遍历函数

清单遍历的三个 `iter_*` 函数是后续校验与生成的入口。

[kernel_inventory/\_\_init\_\_.py:57-74 — iter_kernel_definition_paths](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/kernel_inventory/__init__.py#L57-L74) 同时收集 transformer 与 suite 两类 Definition，但对 suite 做「至少有一个后端实现」的过滤：

```python
transformer_paths = root_path.glob("src/tilegym/transformers/*/kernel_definitions/*.json")
suite_paths = root_path.glob("src/tilegym/suites/*/kernel_definitions/*.json")
paths = set(transformer_paths)
paths.update(
    path for path in suite_paths
    if any((path.parent.parent / "kernel_solutions" / backend).is_dir()
           for backend in _SUITE_BACKENDS)
)
yield from sorted(paths)
```

即：一个 suite 的 Definition 只有在它对应的 `kernel_solutions/<后端>/` 至少存在一个目录时，才会被纳入清单——避免把「光有定义没人实现」的孤儿 Definition 计入。

[kernel_inventory/\_\_init\_\_.py:86-92 — iter_kernel_python_paths](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/kernel_inventory/__init__.py#L86-L92) 只遍历 transformer 的 `kernels/*.py` 模块（suite 的内核可导入性改由其 Solution 入口点测试覆盖）：

```python
def iter_kernel_python_paths(root):
    yield from sorted(Path(root).glob("src/tilegym/transformers/*/kernels/*.py"))
```

校验函数 `validate_definition` / `validate_solution` 会做 FIB 模型校验 + TileGym 自有检查（见 4.3 节），并强制 reference 必须以精确永久链接开头（见 [kernel_inventory/\_\_init\_\_.py:115-139](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/kernel_inventory/__init__.py#L115-L139) 与 [source_contract.py](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/kernel_inventory/source_contract.py)）。

#### 4.2.4 代码实践

**实践目标**：跑通清单的「全量校验」测试，理解它断言了什么。

**操作步骤**：

1. 阅读 [tests/kernel_inventory/test_kernel_inventory.py:403-419](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/tests/kernel_inventory/test_kernel_inventory.py#L403-L419) 中的 `test_all_current_kernel_definitions_validate` 与 `test_all_current_kernel_solutions_validate`。
2. 运行（需要 modeling/transformers 的 dev 环境，见下方说明）：

   ```bash
   pytest tests/kernel_inventory/test_kernel_inventory.py -k "validate or catalog" -v
   ```

**需要观察的现象**：测试遍历仓库内所有 Definition/Solution，逐一断言「文件名 == name」「Solution.name 以 definition 开头」「入口符号存在」等。

**预期结果**：所有用例通过；若某份 JSON 不合规（如 reference 缺少 `# Source:` 永久链接），对应用例会失败并指出具体文件。

> 若缺少 flashinfer-bench 依赖导致 `ImportError`，可改为**源码阅读型实践**：列出 `test_kernel_definition_solution_catalog_is_complete`（[test_kernel_inventory.py:422-439](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/tests/kernel_inventory/test_kernel_inventory.py#L422-L439)）校验的三条「目录完整性」不变式，并说明它们各自防的是什么错误。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `iter_kernel_definition_paths` 对 suite 路径要做「至少有一个后端目录」的过滤，对 transformer 路径却不需要？

**参考答案**：transformer 布局里 Definition 与 Solution 一一平级，存在 Definition 文件通常意味着也存在对应 Solution；而 suite 布局允许多后端，一个 Definition 可能暂时只有定义、尚未有任何后端实现。过滤掉这种「孤儿 Definition」能让清单只包含真正可执行的算子，避免 CI 报「Definition 缺 Solution」的噪音。

**练习 2**：Solution 的 `sources` 为什么用 `{"path": [...]}` 而不直接内嵌源码 `content`？

**参考答案**：源码已经在仓库里以 `.py` 文件存在，内嵌 content 会造成重复、且容易与真实文件不同步。TileGym 用路径引用，在校验或导出给 FIB 时再「物化」（materialize）读取真实文件内容（见 4.3 节 `materialize_solution_sources`），保证单一事实来源。

### 4.3 FIB 模式与 pydantic 模型

#### 4.3.1 概念说明

Definition/Solution 是 JSON，需要严格的 schema 校验。TileGym 的策略是**复用 FlashInfer-Bench 的 pydantic 模型**（`flashinfer_bench.data.Definition` / `Solution`）做主体校验，再叠加上自己的「源码契约」检查。但 flashinfer-bench 是较重的可选依赖，不能让普通 `import tilegym` 就强制安装它。

解法是**懒加载**：`generation.py` 模块顶部直接 `from flashinfer_bench.data import ...`，而 `kernel_inventory/__init__.py` 只在调用 `validate_*` 时才 `from tilegym.kernel_inventory.generation import ...`，并捕获 `ImportError` 给出友好提示（[kernel_inventory/\_\_init\_\_.py:118-128](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/kernel_inventory/__init__.py#L118-L128)）。这样运行时（用内核）与清单生成时（校验 JSON）的依赖彻底分离。

#### 4.3.2 核心流程：TileGym 自有模型

`generation.py` 定义了三个 pydantic 模型来适配 TileGym 的「仅路径 sources」与「cuda-tile 语言」需求，它们校验完后再转成 FIB 原版模型做二次校验：

```text
JSON dict
   │  TileGymSolution.model_validate(...)
   ▼
TileGymSolution  ──┐  自有校验（路径合法、entry_point 在 sources 里、语言合法）
   │               │
   │  .to_fib_solution(repo_root)
   ▼               │  物化 content + 把 cuda-tile 映射成 python
Solution (FIB) ◄───┘  FIB 原版模型再校验一遍
```

#### 4.3.3 源码精读

**模型一：`TileGymSourceFile`** —— 允许 `content` 为 `None` 的源文件引用（[generation.py:39-50](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/kernel_inventory/generation.py#L39-L50)）。它的校验器禁止绝对路径与含 `..` 的路径，防止清单引用仓库之外的文件：

```python
class TileGymSourceFile(BaseModel):
    path: str
    content: str | None = None

    @model_validator(mode="after")
    def _validate_source_path(self):
        raw_path = Path(self.path)
        if not self.path or raw_path.is_absolute() or ".." in raw_path.parts:
            raise ValueError(f"Invalid source path: {self.path}")
        return self
```

**模型二：`TileGymBuildSpec`** —— 描述构建规格（[generation.py:53-82](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/kernel_inventory/generation.py#L53-L82)）。它强制三件事：① `language` 必须是 FIB 支持的语言或 TileGym 自有的 `"cuda-tile"`；② `entry_point` 必须形如 `"<file>::<symbol>"`（恰好一个 `::`）；③ `target_hardware` 必须形如 `SM<major><minor>`（正则 `SM\d{2,}`）：

```python
if self.entry_point.count("::") != 1:
    raise ValueError(f'Invalid entry point format: {self.entry_point}. Expected "<file_path>::<function_name>".')
invalid_targets = [t for t in self.target_hardware if re.fullmatch(r"SM\d{2,}", t) is None]
```

它还提供 `fib_language()`，把 TileGym 的 `"cuda-tile"` 映射到 FIB 最接近的 `"python"`（[generation.py:33-36, 80-82](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/kernel_inventory/generation.py#L33-L36)），这样 FIB 模型不会因为不认识 `cuda-tile` 而报错。

**模型三：`TileGymSolution`** —— 顶层 Solution 模型（[generation.py:85-125](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/kernel_inventory/generation.py#L85-L125)）。它有两个亮点：

- `before` 模式校验器 `_normalize_sources`（[generation.py:95-112](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/kernel_inventory/generation.py#L95-L112)）把 `{"path": ...}` 形式统一转成 `[{"path": ...}]`，兼容 FIB 的 file-object 写法。
- `after` 模式校验器（[generation.py:114-125](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/kernel_inventory/generation.py#L114-L125)）检查「source 路径不重复」与「entry_point 的源文件必须出现在 sources 里」。

物化与导出由 `to_fib_solution`（[generation.py:127-153](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/kernel_inventory/generation.py#L127-L153)）完成：当某 source 的 `content` 为 `None` 时，从 `repo_root` 读取真实文件内容填入，再交给 FIB `Solution.model_validate`。

构造入口 `make_solution` / `make_definition`（[generation.py:178-235](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/kernel_inventory/generation.py#L178-L235)）是给代码生成脚本用的便捷工厂；校验入口 `validate_tilegym_definition_model` / `validate_tilegym_solution_model`（[generation.py:243-254](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/kernel_inventory/generation.py#L243-L254)）是给 CI 用的检查函数。

> **reference 契约**：Definition 的 `reference` 字符串不仅是 Python 代码，还必须以一行或多行 `# Source: <永久链接>` 开头，链接须是 `/blob/<40位commit>/...#L行号` 形式的 GitHub 或 HuggingFace 永久链接（见 [source_contract.py:18-46](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/kernel_inventory/source_contract.py#L18-L46)）。这保证每个算子的参考实现都可追溯到上游某次确切提交，不会随上游变动而漂移。

#### 4.3.4 代码实践

**实践目标**：用 `make_solution` 在内存里构造并校验一份 Solution，观察它对非法输入的拒绝。

**操作步骤**（需 flashinfer-bench；若不可得则改为阅读型）：

```python
# 在仓库根目录运行；需要 modeling/transformers 的 dev 环境装好 flashinfer-bench
import sys, types
from pathlib import Path
REPO = Path.cwd()
# 避免触发 tilegym/__init__.py 的后端初始化
sys.modules.setdefault("tilegym", types.ModuleType("tilegym")).__path__ = [str(REPO/"src/tilegym")]

from tilegym.kernel_inventory.generation import make_solution
from tilegym.kernel_inventory import KernelInventoryError

spec = {
    "language": "cuda-tile",
    "target_hardware": ["SM100"],            # 故意试错时可改成 ["NVIDIA_B200"]
    "entry_point": "src/tilegym/transformers/qwen3_5/kernels/sigmoid_mul.py::sigmoid_mul_cutile",
    "destination_passing_style": False,
}
try:
    sol = make_solution(name="t", definition="d", author="me",
                        spec=spec, sources="src/tilegym/transformers/qwen3_5/kernels/sigmoid_mul.py",
                        repo_root=REPO)
    print("OK:", sol.name)
except KernelInventoryError as e:
    print("被拒:", e)
```

**需要观察的现象**：合法输入打印 `OK`；把 `target_hardware` 改成 `["NVIDIA_B200"]` 后，会被 `SM<major><minor>` 正则拒绝并打印「被拒」。

**预期结果**：与 [test_solution_requires_compute_capability_target_hardware](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/tests/kernel_inventory/test_kernel_inventory.py#L337-L341) 的断言一致。

#### 4.3.5 小练习与答案

**练习 1**：`TileGymBuildSpec.fib_language()` 为什么要把 `"cuda-tile"` 映射成 `"python"`？

**参考答案**：FIB 原版的 `SupportedLanguages` 枚举不认识 TileGym 自有的 `"cuda-tile"` 语言标签。校验 Solution 时要复用 FIB 的 `Solution` 模型，因此先把 `cuda-tile` 临时映射成最接近的 `python`（cuTile 内核确实是 Python DSL），让 FIB 校验通过；TileGym 自己的 `TileGymBuildSpec` 则把 `cuda-tile` 加进了合法语言集合，保留真实标签。

**练习 2**：为什么 `kernel_inventory/__init__.py` 里 `validate_definition` 要用 `try/except ImportError` 包住对 `generation` 的导入？

**参考答案**：`generation.py` 顶部直接 import `flashinfer_bench`，而它是可选重依赖。若 `validate_definition` 在模块顶层就 import `generation`，则任何 `import tilegym.kernel_inventory` 都会强制要求装好 flashinfer-bench。把 import 放进函数体内并捕获 ImportError，可以让「运行时用内核」完全不必装 flashinfer-bench，只有「校验/生成清单」时才需要。

### 4.4 suite 后端 solution 遍历（triton / cutile / cutile_rs）

#### 4.4.1 概念说明

回顾 u10-l1：suites 用「命名空间算子名」（如 `liger.xxx`）接入外部内核库。suites 的清单也要支持「同一 Definition、多后端实现」——这正是 4.2 节 suite 布局里 `kernel_solutions/<后端>/` 子目录的用途。

TileGym 把可在 suite 下出现的后端收敛成一个常量（[kernel_inventory/\_\_init\_\_.py:44-45](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/kernel_inventory/__init__.py#L44-L45)）：

```python
_SUITE_BACKENDS = ("triton", "cutile", "cutile_rs")
```

`cutile_rs`（cuTile-rs，Rust FFI 后端，见 u7-l3）本轮被显式纳入清单遍历，意味着 suite 可以同时提供 triton、cutile、cutile-rs 三种实现，清单会一并发现并校验。

#### 4.4.2 核心流程：遍历逻辑

```text
iter_kernel_solution_paths(root):
    patterns = (
      "src/tilegym/transformers/*/kernel_solutions/*.json",      # transformer 布局
      "src/tilegym/suites/*/kernel_solutions/triton/*.json",     # suite: triton
      "src/tilegym/suites/*/kernel_solutions/cutile/*.json",     # suite: cutile
      "src/tilegym/suites/*/kernel_solutions/cutile_rs/*.json",  # suite: cutile_rs ← 本轮纳入
    )
    → 用 set 去重后排序输出
```

对单个 Definition，[iter_solution_paths_for_definition](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/kernel_inventory/__init__.py#L95-L112) 会先查 transformer 的平级 Solution，再遍历三个 suite 后端子目录，把同一 Definition 的所有 Solution 都列出来。

#### 4.4.3 源码精读

[kernel_inventory/\_\_init\_\_.py:77-83 — iter_kernel_solution_paths](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/kernel_inventory/__init__.py#L77-L83) 用 `_SUITE_BACKENDS` 拼出所有 suite 后端的 glob 模式：

```python
def iter_kernel_solution_paths(root):
    patterns = ("src/tilegym/transformers/*/kernel_solutions/*.json",) + tuple(
        f"src/tilegym/suites/*/kernel_solutions/{backend}/*.json" for backend in _SUITE_BACKENDS
    )
    yield from _iter_paths(root_path, patterns)
```

[kernel_inventory/\_\_init\_\_.py:294-296 — \_iter_paths](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/kernel_inventory/__init__.py#L294-L296) 用集合去重后排序，保证输出稳定（这对 CI 的可复现性很重要）：

```python
def _iter_paths(root, patterns):
    paths = {path for pattern in patterns for path in root.glob(pattern)}
    yield from sorted(paths)
```

[kernel_inventory/\_\_init\_\_.py:95-112 — 给定 Definition 找其全部 Solution](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/kernel_inventory/__init__.py#L95-L112) 先在 transformer 位置找，再在三个后端子目录找：

```python
for backend in _SUITE_BACKENDS:
    suite_solution = definition.parent.parent / "kernel_solutions" / backend / definition.name
    if suite_solution.is_file():
        yield suite_solution
```

这样一份 suite Definition 即可对应 1～3 份后端 Solution，全部被 CI 的 `test_all_current_kernel_solutions_validate` 校验。

#### 4.4.4 代码实践

**实践目标**：亲手遍历当前仓库的清单，看清楚现在有哪些后端子目录被命中。

**操作步骤**（无需 GPU，纯文件遍历）：

```bash
python -c "
import sys, types
from pathlib import Path
pkg = types.ModuleType('tilegym'); pkg.__path__=['src/tilegym']; sys.modules['tilegym']=pkg
from tilegym.kernel_inventory import iter_kernel_solution_paths
from collections import Counter
c = Counter()
for p in iter_kernel_solution_paths('.'):
    # 用路径里是否含 triton/cutile/cutile_rs 区分 suite 后端 vs transformer
    parts = p.parts
    backend = 'transformer'
    for b in ('triton','cutile_rs','cutile'):
        if b in parts: backend = b; break
    c[backend] += 1
print(dict(c))
"
```

**需要观察的现象**：输出一个字典，统计当前仓库各类后端 Solution 的数量。

**预期结果**：当前仓库以 `transformer` 布局为主（如 `qwen3_5`、`olmo3`、`olmoe` 下的若干算子），`triton`/`cutile`/`cutile_rs` 计数多为 0（因 suites 尚未落地按后端分目录的 Solution）。代码已为这三类后端预留了遍历通道，一旦 suites 落地对应目录即被自动发现。

#### 4.4.5 小练习与答案

**练习 1**：若要新增一个名为 `tilecpp` 的 suite 后端清单支持，最少要改哪里？

**参考答案**：在 [kernel_inventory/\_\_init\_\_.py:45](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/kernel_inventory/__init__.py#L45) 的 `_SUITE_BACKENDS` 元组里加 `"tilecpp"`。由于 `iter_kernel_solution_paths`、`iter_solution_paths_for_definition`、`iter_kernel_definition_paths` 的 suite 过滤都基于这个常量生成 glob 模式，加一处即可让三类遍历与校验全部覆盖新后端。

**练习 2**：为什么遍历函数用 `set` 去重再 `sorted`？

**参考答案**：glob 的模式之间可能重叠（虽然这里不太会），去重避免同一文件被产出两次；排序让遍历顺序与文件系统无关、跨机器稳定，CI 失败时可复现。

### 4.5 autotune 全局开关

#### 4.5.1 概念说明

许多 cuTile 内核在启动前会做自动调优（autotune）：在多个候选配置里实测、择优缓存（详见 u5-l3）。调优耗时，调试或基准时可能想关掉它。TileGym 用**单一环境变量** `TILEGYM_DISABLE_AUTOTUNE` 作为进程级总开关，并由两个查询函数封装读法。

这与 u2-l3 的 `selector.py` 共享同一种工程哲学：**策略集中、读法唯一**——业务代码不直接读环境变量，只调函数。

#### 4.5.2 核心流程：取值语义

```text
TILEGYM_DISABLE_AUTOTUNE 未设置        → is_autotune_disabled()=False → 启用调优（默认）
TILEGYM_DISABLE_AUTOTUNE=1/true/yes/on → is_autotune_disabled()=True  → 禁用调优
TILEGYM_DISABLE_AUTOTUNE=0/false/no/off → is_autotune_disabled()=False → 启用调优
TILEGYM_DISABLE_AUTOTUNE=其他           → 抛 ValueError（fail-fast）
```

注意「未设置」与「显式 `0`」语义相同，都表示启用；这是「默认开」的开关。

#### 4.5.3 源码精读

[autotune.py:5-33 — is_autotune_disabled](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/autotune.py#L5-L33) 把取值集合写成两个 `frozenset`，未命中者直接抛错：

```python
DISABLE_AUTOTUNE_ENV = "TILEGYM_DISABLE_AUTOTUNE"
_DISABLE_AUTOTUNE_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_DISABLE_AUTOTUNE_FALSE_VALUES = frozenset({"0", "false", "no", "off"})

def is_autotune_disabled() -> bool:
    import os
    disable_flag = os.environ.get(DISABLE_AUTOTUNE_ENV)
    if disable_flag is None:
        return False
    disable_flag = disable_flag.strip().lower()
    if disable_flag in _DISABLE_AUTOTUNE_TRUE_VALUES:
        return True
    if disable_flag in _DISABLE_AUTOTUNE_FALSE_VALUES:
        return False
    ... raise ValueError(...)
```

[autotune.py:36-38 — is_autotune_enabled](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/autotune.py#L36-L38) 只是前者的逻辑取反：

```python
def is_autotune_enabled() -> bool:
    return not is_autotune_disabled()
```

**谁在用这个开关？** 全仓库搜索 `is_autotune_enabled` / `is_autotune_disabled` 的调用点，可见它同时服务于核心 ops 与 suites：

| 调用方 | 文件 | 用法 |
| --- | --- | --- |
| FMHA / decode 注意力 | `ops/cutile/attention.py` | `if is_autotune_disabled(): 跳过调优` |
| MLA | `ops/cutile/mla.py` | 同上 |
| RoPE / LayerNorm / RMSNorm | `ops/cutile/rope.py`、`layer_norm_legacy.py` | 同上 |
| 门控增量规则 | `ops/cutile/recurrent_gated_delta_rule.py` | 同上 |
| 稀疏 MLA / attention sink | `ops/cutile/experimental/*.py` | 同上 |
| flashinfer suite | `suites/flashinfer/cutile/gemm/*.py`、`rope_quantize_fp8.py`、`fmha_decode_bsr.py` | `enable_autotune = is_autotune_enabled()` |

**一个重要例外**：`ops/cutile/matmul.py` **不**接入这个开关——它永远调优（其内部有自己的 `_matmul_autotune_configs` 机制，详见 u5-l3）。这与 suites 内核（如 `ragged_bmm` / `masked_bmm` / `rope_quantize_fp8`）才接全局开关形成对照。判断一个内核是否受 `TILEGYM_DISABLE_AUTOTUNE` 控制，看它有没有 import 这两个查询函数。

#### 4.5.4 代码实践

**实践目标**：观察开关对一次带 autotune 内核调用的影响。

**操作步骤**（需 GPU；否则改为阅读型）：

```bash
# 1) 默认（启用调优）
python -c "
import tilegym, torch
from tilegym.ops import rms_norm
x = torch.randn(2048, 4096, device='cuda', dtype=torch.bfloat16)
w = torch.ones(4096, device='cuda', dtype=torch.bfloat16)
%timeit -r1 -n1 rms_norm(x, w) if hasattr(__builtins__,'timeit') else rms_norm(x,w)
" 2>&1 | tail -5

# 2) 禁用调优
TILEGYM_DISABLE_AUTOTUNE=1 python -c "
import tilegym, torch
from tilegym.ops import rms_norm
x = torch.randn(2048, 4096, device='cuda', dtype=torch.bfloat16)
w = torch.ones(4096, device='cuda', dtype=torch.bfloat16)
rms_norm(x, w)
print('done')
" 2>&1 | tail -5
```

**需要观察的现象**：默认下首次调用可能触发候选配置的实测择优（取决于该算子是否接全局开关）；`TILEGYM_DISABLE_AUTOTUNE=1` 时则跳过该过程。

**预期结果**：两次输出数值一致；调优被跳过时首次调用更快、但可能选不到最优配置。

> 若无 GPU，改为**源码阅读型实践**：在 `ops/cutile/attention.py` 中找到 `is_autotune_disabled()` 的调用点（如 [attention.py:759](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/attention.py#L759)），说明它在为真时走哪条降级分支。再用 `TILEGYM_DISABLE_AUTOTUNE=maybe` 运行 `python -c "from tilegym.autotune import is_autotune_disabled; is_autotune_disabled()"`，观察是否如预期抛 `ValueError`。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `is_autotune_disabled` 在函数内部 `import os`，而不是在模块顶部？

**参考答案**：注释写明「让函数自包含」。这样在模块加载时不产生副作用，也方便单元测试在打桩/猴子补丁 `os.environ` 时不受模块级导入顺序干扰。每次调用现读环境变量，也意味着运行中修改 `os.environ` 能立即生效。

**练习 2**：传入非法值（如 `TILEGYM_DISABLE_AUTOTUNE=maybe`）为什么选择抛 `ValueError` 而不是静默当作「启用」？

**参考答案**：fail-fast。静默忽略会让用户以为关掉了调优、实际却没关，导致基准或调试结果失真且难排查。抛错把「拼写错误」立即暴露，符合 TileGym「策略集中、读法唯一、行为可预测」的一贯风格。

## 5. 综合实践

把本讲四块知识串起来，完成下面这个「为实验性内核补齐清单」的小任务。

**场景**：假设你要把 `ops/cutile/experimental/swa_attention.py` 里的滑动窗口注意力（u6-l4 讲过，它挂着 `@experimental_kernel`）登记进 kernel_inventory，并让它受全局 autotune 开关控制。

**任务**：

1. **告警链路**：在 [swa_attention.py:39](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/experimental/swa_attention.py#L39) 确认 `@experimental_kernel` 的位置。画出「用户调用 → `_patched_launch` → `warn_once` → `_original_launch`」的完整链路，并解释为什么作者不需要在内核函数体里写任何日志代码。
2. **清单契约**：仿照 [qwen3_5_sigmoid_mul.json](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/transformers/qwen3_5/kernel_definitions/qwen3_5_sigmoid_mul.json)，为 swa_attention **起草**一份 Definition（声明 axes/inputs/outputs/reference）。要求 `reference` 以一行 `# Source: <GitHub 永久链接>#L行号` 开头（可指向本仓库 `swa_attention.py` 的某次提交）。再起草一份 Solution，`spec.language` 用 `"cuda-tile"`，`target_hardware` 用 `SMxx` 格式。
3. **遍历验证**：说明若把它放进 suite 布局（如 `src/tilegym/suites/flashinfer/kernel_definitions/` 与 `kernel_solutions/cutile/`），`iter_kernel_solution_paths` 会如何发现它；以及 `test_all_current_kernel_solutions_validate` 会校验它哪些不变式。
4. **autotune**：说明它要受 `TILEGYM_DISABLE_AUTOTUNE` 控制，需要在内核文件里加哪一行 import、用哪个函数判断。

**验收**：把上述四点的答案写成一份简短文档；其中 Definition/Solution 草稿可用 `make_definition` / `make_solution`（需 dev 环境）或人工对照 schema 自检。若无法在本机校验，明确标注「待本地验证」。

## 6. 本讲小结

- `experimental_kernel` 装饰器（置于 `@ct.kernel` 之外）只给内核对象挂 `_tracked_message` 属性，**不改动内核函数体**；真正告警由 `_apply_patch()` 在 `import tilegym` 时安装的 `ct.launch` monkey-patch 完成。
- 告警有**两层去重**：补丁清空 `kernel._tracked_message`（per-内核对象），`warn_once` 按 `category:message` 全局去重（进程级）。`reset_tracking()` 目前是空占位。
- kernel_inventory 用 FIB 的 **Definition（规格）/ Solution（实现）** 两类 JSON 组织清单，磁盘分 transformer（平级）与 suite（按 `triton`/`cutile`/`cutile_rs` 后端子目录）两种布局。
- `generation.py` 的三个 pydantic 模型 `TileGymSourceFile` / `TileGymBuildSpec` / `TileGymSolution` 负责 TileGym 自有校验（仅路径 sources、`cuda-tile` 语言、`SMxx` 硬件、entry_point 在 sources 中），再转交 FIB 模型二次校验；该模块懒加载，运行时不强依赖 flashinfer-bench。
- `iter_kernel_solution_paths` 通过 `_SUITE_BACKENDS = ("triton","cutile","cutile_rs")` 生成 glob 模式，**本轮把 cutile_rs 显式纳入** suite 后端遍历；遍历结果用 set 去重后排序以保证可复现。
- `TILEGYM_DISABLE_AUTOTUNE` 是单一进程级开关（`1/true/yes/on` 禁用、`0/false/no/off` 或未设置启用、非法值抛错），由 `is_autotune_enabled()` / `is_autotune_disabled()` 封装；核心多数注意力/归一化内核与 flashinfer suite 内核受其控制，但 `matmul.py` 永远调优、不接此开关。

## 7. 下一步学习建议

- **接 u10-l4**：本讲的清单机制与 autograd/FIB 模型，将在「Liger 训练内核族」中用于理解训练损失族如何登记进 suites；可对照 `suites/liger/` 的算子组织。
- **回看 u8-l3**：kernel coverage 报告与 kernel_inventory 是互补的两套治理工具——前者在运行时统计「GPU 时间花在哪些内核」，后者在静态层面登记「仓库有哪些内核及其规格」。
- **深入 FIB**：若你对 Definition/Solution 的 schema 细节感兴趣，可阅读 [docs/flashinfer-trace/definition.mdx](https://github.com/flashinfer-ai/flashinfer-bench/blob/main/docs/flashinfer-trace/definition.mdx) 与 `solution.mdx`（schema URL 见 [kernel_inventory/\_\_init\_\_.py:34-37](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/kernel_inventory/__init__.py#L34-L37)），理解 TileGym 为何选择「复用 FIB 模型 + 路径化 sources」的折中。
- **动手扩展**：尝试为某个尚未登记的 experimental 内核（如 `experimental/mhc.py`）补一份 Definition + Solution，跑 `pytest tests/kernel_inventory` 验证。
