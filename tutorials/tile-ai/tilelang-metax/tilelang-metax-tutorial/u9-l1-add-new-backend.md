# 扩展：新增目标后端

## 1. 本讲目标

本讲是「扩展与内部机制」单元的第一讲。学完后你应当能够：

- 说清「给 TileLang 加一个新 target 后端」到底要改哪些地方，理解它不是一锤子买卖，而是**在两张注册表（Python 侧 + C++ 侧）里填一串具名插槽**。
- 掌握 `register_lazy_device_codegen` / `register_lazy_execution_backends` 的**懒加载**机制：为什么只存一个 import 路径字符串，真正的注册推迟到首次使用。
- 理解 `register_target_detector` 如何把新后端纳入 `auto` 自动探测链，以及探测顺序由谁决定。
- 理解 C++ 侧 `src/backend/common/target_utils.cc` 的「公共分发 + 后端自有实现」模式，知道新增后端要在这里挂一个分支。

我们把 **MACA 后端当作现成的样板**来逆向拆解——因为它是上游 `tilelang` 的一个 fork 为 MetaX GPU **整端新增**出来的真实后端，每一处接线都看得见、摸得着。

## 2. 前置知识

阅读本讲前，建议你已经学过：

- **u1-l3**：仓库目录结构，知道 `tilelang/` 是 Python 前端、`src/` 是 C++ 编译核心，两侧都呈「公共层 + 后端自有层」双层布局。
- **u3-l1**：target 体系，知道 target = kind + attrs，知道 `determine_target` 与 `auto` 检测。
- **u5-l3**：CUDA/HIP codegen，知道 `CodeGenTileLangCUDA` 由 `target.build.tilelang_cuda` 全局函数实例化、`intrin_rule` 与 `target_utils` 的关系。
- **u7-l1**：MACA 后端架构总览，知道「运行时四件套 + Python 三注册点」、`warp_size=64`、`mxcc`/`mcbin`。

几个本讲会反复用到、但不再展开的术语：

- **target kind**：平台的类型名，如 `cuda`/`hip`/`maca`，由 C++ 的 `TVM_REGISTER_TARGET_KIND` 注册。
- **注册表（registry）**：一个按名字查找的全局字典/映射。Python 侧是模块级 `dict`，C++ 侧是 `TVM_REGISTER_*` 宏 + `TVM_FFI_STATIC_INIT_BLOCK` 在静态初始化期填充的全局表。
- **FFI（Foreign Function Interface）**：Python 与 C++(`libtilelang.so`) 之间的调用桥，TVM 用 `tvm.ffi.get_global_func("名字")` 按名取得 C++ 注册的可调用对象。
- **DLDeviceType**：DLPack 里定义的设备类型枚举（如 `kDLCUDA`/`kDLROCm`）。本仓库定制的 TVM 在该枚举里新增了 `kDLMACA`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `tilelang/backend/__init__.py` | **Python 注册中枢**：集中调用所有 `register_lazy_*`，把每个 target kind 绑到它的后端模块。 |
| `tilelang/backend/device_codegen.py` | 设备 codegen 注册表 + 懒加载逻辑（`register_lazy_device_codegen`、`resolve_device_codegen`）。 |
| `tilelang/backend/execution_backend.py` | 执行后端注册表 + 懒加载逻辑（`register_lazy_execution_backends`、`resolve_execution_backend_spec`）。 |
| `tilelang/backend/target.py` | target 探测器/归一化器注册表 + `determine_target`/`auto_detect_target` 主流程。 |
| `tilelang/maca/__init__.py` | MACA 后端 Python 包入口：import 时触发本后端的所有 `register_*`。 |
| `tilelang/maca/target.py` | MACA 的 `register_target_detector` 与可用性检测。 |
| `tilelang/maca/codegen.py` | MACA 的 `register_device_codegen`，桥到 C++ `target.build.tilelang_maca`。 |
| `tilelang/maca/execution_backend.py` | MACA 的执行后端（tvm_ffi/mcrtc/cython/cutedsl）注册。 |
| `tilelang/maca/pipeline.py` | MACA 的 pass 流水线 `register_pipeline(PassPipeline("maca", ...))`。 |
| `tilelang/__init__.py` | 顶层包入口：`from . import maca` 触发 MACA 注册链。 |
| `src/maca/runtime/maca_target_kind.cc` | **C++ 注册中枢**：`TVM_REGISTER_TARGET_KIND("maca", kDLMACA)` + 属性 + canonicalizer。 |
| `src/maca/target_utils.cc` / `.h` | C++ 能力谓词：`TargetIsMaca`、`TargetMacaGetWarpSize`，并注册为 `tl.TargetIsMaca` 等 FFI。 |
| `src/backend/common/target_utils.cc` / `.h` | **统一分发层**：`TargetHasAsyncCopy` 按 `TargetIsCuda/Rocm/Maca` 分发。 |
| `src/maca/codegen/rt_mod_maca.cc` | C++ 设备 codegen 入口 `BuildTileLangMACA`，注册为 `target.build.tilelang_maca`。 |
| `src/maca/runtime/maca_device_api.cc` | `MACADeviceAPI`，注册为 `device_api.maca`。 |
| `src/maca/CMakeLists.txt` | MACA 源码清单 + `USE_MACA` 开关 + 两层条件编译。 |
| `CMakeLists.txt` | 顶层：读取 `USE_MACA` 环境变量并 `include(src/maca/CMakeLists.txt)`。 |

> 记忆口诀：**Python 侧填三张表（detector / lazy-codegen / lazy-execution + pipeline），C++ 侧填四件套（target kind / device api / build 函数 / target_utils）**，两边用同名 FFI 字符串对齐。

## 4. 核心概念与源码讲解

### 4.1 backend 注册：一张注册表串起一个后端

#### 4.1.1 概念说明

很多人以为「加一个后端」就是写 codegen。其实 codegen 只是结果。**真正的工程骨架是一组按名字查找的全局注册表**：编译器在运行时靠「target kind 名字」去这些表里查「该用哪个 codegen、哪个执行后端、哪条流水线、能不能自动探测」。你写的新后端代码本身不会自动被发现，**必须显式把自己登记进这些表**。

MACA 后端就是这套机制最完整的样板：它把 `maca` 这个名字同时登记进了 Python 的 4 张表和 C++ 的 4 张表。理解了它在哪里登记，你照葫芦画瓢就能加 `mygpu`。

#### 4.1.2 核心流程

一个 target 从「被用户写出」到「能编译运行」，要顺次查 4 张表：

```text
用户写 target={"kind":"maca"}
        │
        ▼
① determine_target ──查──▶ register_target_detector   （这个 kind 谁来探测/归一化）
        │
        ▼
② engine.lower ──查──▶ register_pipeline               （这个 kind 跑哪条 pass 流水线）
        │
        ▼
③ device codegen ──查──▶ register_device_codegen       （这个 kind 用哪个 codegen）
        │
        ▼
④ 产出 kernel ──查──▶ register_execution_backend       （这个 kind 用哪种执行后端启动）
```

这 4 张表的**填充时机**分两种：

- **懒加载（lazy）**：codegen、execution backend 用懒注册——先只存一个模块路径字符串，等该 target 真被用到时才 `import` 那个模块（见 4.2）。
- **立即加载（eager）**：target detector、pipeline 在后端模块一被 import 时就当场登记。

而**后端模块何时被 import**？由 `tilelang/__init__.py` 顶层 `from . import maca` 决定——这是整个 fork 挂载 MACA 的那一行（详见源码精读）。

#### 4.1.3 源码精读

**(1) Python 注册中枢集中登记**。[tilelang/backend/\_\_init\_\_.py:36-50](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/backend/__init__.py#L36-L50) 把每个 target kind 绑到它的后端模块路径：

```python
register_lazy_execution_backends("cuda", "tilelang.cuda.execution_backend")
register_lazy_execution_backends("maca", "tilelang.maca.execution_backend")   # ← MACA 执行后端
register_lazy_execution_backends("hip",  "tilelang.rocm.execution_backend")
...
register_lazy_device_codegen("maca", "tilelang.maca.codegen")                 # ← MACA 设备 codegen
register_lazy_device_codegen("cuda", "tilelang.cuda.codegen")
...
```

注意这里的对称美：每一行就是「target kind → Python 模块路径」的映射。要加 `mygpu`，就是在这里加两行 `register_lazy_*("mygpu", "tilelang.mygpu.xxx")`。MACA 在执行后端表里出现两次（L37 与 L42）只是冗余登记，`override` 语义会覆盖，无副作用。

**(2) MACA 后端包入口触发全部 eager 注册**。[tilelang/maca/\_\_init\_\_.py:1-6](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/__init__.py#L1-L6)：

```python
from . import intrinsics   # noqa: F401
from . import op           # noqa: F401
from . import pipeline     # noqa: F401   ← register_pipeline 在此触发
from . import target       # noqa: F401   ← register_target_detector 在此触发
from . import execution_backend  # noqa: F401
from . import transform    # noqa: F401
```

一旦这个包被 import，它的子模块就各自调用 `register_*` 把自己登记进对应表。这正是懒加载机制「import 即注册」的落点（见 4.2）。

**(3) 顶层包入口挂载 MACA**。[tilelang/\_\_init\_\_.py:211-215](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/__init__.py#L211-L215)：

```python
from . import cpu as cpu
from . import cuda as cuda
from . import rocm as rocm
from . import metal as metal
from . import maca as maca   # ← metax 分支挂载 MACA 后端的那一行
```

这行 `import` 会执行 `tilelang/maca/__init__.py`，进而把 MACA 的 detector、pipeline 全部 eager 登记好。**删掉这一行，MACA 后端在 Python 侧就「不存在」了**——这就是它的总开关。

#### 4.1.4 代码实践

**实践目标**：亲眼确认「后端 = 一串注册表登记」。

**操作步骤**：

1. 在能 `import tilelang` 的环境里，运行：
   ```python
   import tilelang
   from tilelang.backend import list_target_detectors
   print(list_target_detectors())
   ```
2. 临时把 [tilelang/\_\_init\_\_.py:215](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/__init__.py#L215) 的 `from . import maca as maca` 注释掉（**仅本地实验，勿提交**），重新 `import tilelang` 后再打印探测器列表。

**需要观察的现象**：第 1 步应输出包含 `cuda`、`hip`、`metal`、`maca` 的元组；第 2 步后 `maca` 应从列表中消失。

**预期结果**：证明 MACA 后端在 Python 侧的全部可见性，都源自那一行 `import`。

> ⚠️ 若无法本地验证（如无 MetaX 设备/SDK），明确写「待本地验证」，不要假装跑过。

#### 4.1.5 小练习与答案

**练习 1**：`tilelang/backend/__init__.py` 里 `register_lazy_device_codegen` 把 `maca` 绑到了哪个模块路径？为什么不是 `tilelang.maca` 而是更深层？

**参考答案**：绑到 `tilelang.maca.codegen`（[device codegen 表](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/backend/__init__.py#L44-L50)）。因为懒加载的设计目标是「按需 import 最小子模块」——只需要 codegen 时就不必把整个 `tilelang.maca` 包（含 intrinsics/op 等可能较重的依赖）全部加载。

**练习 2**：如果想新增一个 `mygpu` 后端，`tilelang/backend/__init__.py` 至少要加哪几行？

**参考答案**：至少两行——`register_lazy_execution_backends("mygpu", "tilelang.mygpu.execution_backend")` 与 `register_lazy_device_codegen("mygpu", "tilelang.mygpu.codegen")`。

---

### 4.2 lazy 注册：按需加载的执行/codegen 后端

#### 4.2.1 概念说明

「懒加载」解决一个朴素问题：**MACA 后端的 Python 代码依赖 MetaX SDK、`mxcc` 等重依赖，CUDA 后端依赖 `nvrtc`/torch。如果用户只是想编一个 CUDA kernel，凭什么要被迫 import MACA 的模块、触发它的依赖检查？**

答案是不触发。`register_lazy_*` 只往表里写一个**字符串**（模块路径），真正的 import 推迟到「这个 target 第一次被用到」时。于是 import `tilelang` 永远是轻量的，各后端按需出场。

#### 4.2.2 核心流程

```text
启动期（import tilelang）：
  register_lazy_device_codegen("maca", "tilelang.maca.codegen")
        │  只写字符串到 _LAZY_DEVICE_CODEGENS["maca"]
        ▼
（用户编译一个 cuda kernel，maca 表项从未被触发）

首次用到 maca：
  resolve_device_codegen(Target("maca"))
        │
        ▼
  _ensure_device_codegens_loaded("maca")
        │  发现 "maca" 不在 _LOADED 集合
        ▼
  import_module("tilelang.maca.codegen")   ← 此时才真正 import 重依赖
        │  模块顶层执行 register_device_codegen("maca", ...)
        ▼
  _DEVICE_CODEGENS["maca"] 填充完成，返回匹配的 DeviceCodegen
```

关键点：**「登记路径」和「真正注册」是两个分离的动作**，中间隔了一次按需 `import_module`。`_LOADED` 集合保证每个 kind 只 import 一次。

#### 4.2.3 源码精读

**(1) 懒注册：只存字符串**。[tilelang/backend/device_codegen.py:69-82](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/backend/device_codegen.py#L69-L82)：

```python
def register_lazy_device_codegen(target_kind: str, import_path: str) -> None:
    """Register a backend module to import when its target kind is first used."""
    _LAZY_DEVICE_CODEGENS[target_kind] = import_path
    _LOADED_DEVICE_CODEGENS.discard(target_kind)   # 标记为「尚未真正加载」

def _ensure_device_codegens_loaded(target_kind: str) -> None:
    if target_kind in _LOADED_DEVICE_CODEGENS:
        return
    import_path = _LAZY_DEVICE_CODEGENS.get(target_kind)
    if import_path is not None:
        import_module(import_path)                 # ← 此刻才 import
    _LOADED_DEVICE_CODEGENS.add(target_kind)
```

**(2) 解析时触发加载并匹配**。[tilelang/backend/device_codegen.py:102-110](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/backend/device_codegen.py#L102-L110)：`resolve_device_codegen` 先 `_ensure_device_codegens_loaded` 再从已填充的 `_DEVICE_CODEGENS` 里按 `codegen.matches(target)` 过滤。

**(3) 执行后端同理**。[tilelang/backend/execution_backend.py:60-70](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/backend/execution_backend.py#L60-L70) 的 `register_lazy_execution_backends` / `_ensure_execution_backends_loaded` 是同一套机制的复制粘贴。

**(4) MACA 设备 codegen 模块：import 即登记，并桥到 C++**。[tilelang/maca/codegen.py:12-21](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/codegen.py#L12-L21)：

```python
register_device_codegen(
    "maca",
    DeviceCodegen(
        "maca",
        build=global_func_device_codegen("target.build.tilelang_maca"),
        build_without_compile=global_func_device_codegen("target.build.tilelang_maca_without_compile"),
        supports_target=_is_plain_maca_target,
    ),
    override=True,
)
```

这里 `global_func_device_codegen("target.build.tilelang_maca")`（[device_codegen.py:18-24](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/backend/device_codegen.py#L18-L24)）是 Python→C++ 的关键桥梁：它产出一个回调，回调里 `tvm.ffi.get_global_func("target.build.tilelang_maca")(mod, target)`。而 `target.build.tilelang_maca` 正是 C++ 侧 `BuildTileLangMACA` 注册的全局函数名（见 4.4）。**两边的名字必须一字不差地对上**，否则解析时报「No device codegen registered」。

> 补一个细节：MACA 提供了 `build` 与 `build_without_compile` 两条路径（[rt_mod_maca.cc:142-169](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/rt_mod_maca.cc#L142-L169)）。后者只产出 MACA 源码字符串、不调 `mxcc` 真编译，这正是 u3-l3 提到的「无设备时只取源码」能力的落点。

#### 4.2.4 代码实践

**实践目标**：体会「字符串登记」与「真正 import」的分离。

**操作步骤**：

1. 读 [tilelang/backend/device_codegen.py:69-82](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/backend/device_codegen.py#L69-L82)，回答：调用 `register_lazy_device_codegen("mygpu", "tilelang.mygpu.codegen")` 后，`_DEVICE_CODEGENS["mygpu"]` 有没有内容？
2. 跟踪调用链：`resolve_device_codegen(target)` → `_matching_device_codegens` → `_ensure_device_codegens_loaded`。在 `_ensure_device_codegens_loaded` 的 `import_module(import_path)` 一行旁加注释，说明它产生的副作用。

**需要观察的现象**：第 1 步答案是「没有」——此时只有 `_LAZY_DEVICE_CODEGENS["mygpu"]` 有字符串。

**预期结果**：能口述「懒注册只填路径表；真正的 `DeviceCodegen` 对象要到首次 `resolve` 时 import 模块才出现」。

> 待本地验证：若你真要试验，可在一个干净 REPL 里 `import tilelang` 后检查 `tilelang.backend.device_codegen._LAZY_DEVICE_CODEGENS` 的键。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `register_target_detector`（eager）不像 `register_lazy_device_codegen`（lazy）那样只存字符串？

**参考答案**：detector 是一个**纯 Python 闭包**，本身不依赖重 SDK（MACA 的 detector 只是尝试 `mxcc.find_maca_path()` 并捕获异常，见 4.3），且 `auto` 探测要在 `import tilelang` 后立即可用，所以直接登记可调用对象。而 codegen/execution backend 模块会连带 import torch、SDK 适配器等重依赖，必须延迟。

**练习 2**：`_LOADED_DEVICE_CODEGENS` 集合的作用是什么？如果删掉它会怎样？

**参考答案**：去重，保证每个 target kind 的后端模块**只 import 一次**。删掉后每次 `resolve_device_codegen` 都会重复 `import_module`——虽然 Python 的 import 缓存会让重复 import 很快，但 `register_device_codegen(..., override=False)` 会因重名抛 `ValueError`。

---

### 4.3 target detector：auto 检测与显式输入

#### 4.3.1 概念说明

`target detector` 回答的问题是：「当用户什么都不指定（`target="auto"`）时，编译器凭什么知道该用 MACA？」答案是一张**探测器字典** `_TARGET_DETECTORS`，按 **Python dict 的插入顺序**逐个调用，**第一个返回非 `None` 的胜出**（短路）。

与 detector 配套的还有 `target normalizer`：当用户**显式**给出 target（字符串/字典/Target 对象）时，归一化器有机会把它改写成标准形态。MACA 后端只用了 detector，没用 normalizer（对比之下 cuda/rocm 用了 normalizer 做字符串归一化）。

#### 4.3.2 核心流程

```text
determine_target(target="auto")
        │
        ├─ 取 Target.current()；若 None：
        ▼
  auto_detect_target()
        │
        ▼
  for spec in _TARGET_DETECTORS.values():   ← 按插入顺序
        detected = spec.detect()
        if detected is not None:
            return detected                  ← 短路，后面不再试
```

插入顺序由 [tilelang/\_\_init\_\_.py:211-215](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/__init__.py#L211-L215) 的 import 顺序决定：`cuda → rocm(hip) → metal → maca`。所以 **MACA 是 auto 链的最后一档**——只有前面三档都返回 `None`（机器上没有 CUDA、没有 ROCm、没有 Metal）时才轮到它。

#### 4.3.3 源码精读

**(1) 探测器注册表与短路遍历**。[tilelang/backend/target.py:28-78](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/backend/target.py#L28-L78)：

```python
_TARGET_DETECTORS: dict[str, TargetDetectorSpec] = {}
...
def register_target_detector(name, detect, *, override=False):
    ...
    _TARGET_DETECTORS[name] = spec

def auto_detect_target() -> TargetInput:
    errors: list[str] = []
    for spec in _TARGET_DETECTORS.values():     # ← 插入顺序
        try:
            detected = spec.detect()
        except Exception as err:
            errors.append(...); continue
        if detected is not None:
            return detected                     # ← 短路
    raise ValueError(...)
```

注意 `override=True` 的含义：后注册的同名探测器会覆盖先注册的。MACA 在 [tilelang/maca/target.py:48](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/target.py#L48) 用 `override=True` 登记自己。

**(2) MACA 探测器的判据**。[tilelang/maca/target.py:29-37](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/target.py#L29-L37)：

```python
def _detect_maca_target() -> Target | str | None:
    import torch
    if torch.version.hip is not None:        # ① 若 torch 是 HIP 版，让位给 ROCm
        return None
    if not check_maca_availability():        # ② 找不到 MACA SDK 路径
        return None
    return Target("maca")                    # ③ 否则选中 MACA
```

两条让位逻辑很关键：

- ① **HIP 版 torch 让位**：MACA 与 ROCm 都可能出现在类 Unix + GPU 环境，若 torch 报告自己是 HIP 构建，说明这是 ROCm 机器，MACA 主动退出，避免抢夺。
- ② **`check_maca_availability`**（[target.py:14-26](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/target.py#L14-L26)）尝试 `mxcc.find_maca_path()`，捕获异常返回 `False`。即「系统上装没装 MACA SDK」是探测的最终判据，落在环境变量 `MACA_PATH`/`MACA_HOME` 上。

#### 4.3.4 代码实践

**实践目标**：理解探测顺序与让位逻辑。

**操作步骤**：

1. 用 `list_target_detectors()` 打印探测器顺序，确认 `maca` 在最后。
2. 阅读 [tilelang/maca/target.py:29-37](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/target.py#L29-L37)，回答两个问题：(a) 为什么 MACA 要在 `torch.version.hip is not None` 时返回 `None`？(b) 如果一台机器同时装了 CUDA 和 MACA SDK，`auto` 会选谁？

**需要观察的现象 / 预期结果**：

1. 探测器顺序为 `cuda, hip, metal, maca`（按 import 次序）。
2. (a) 避免与 ROCm 抢夺——HIP 版 torch 强烈暗示这是 ROCm 环境；(b) 选 **cuda**，因为它排在最前且 detector 返回非 `None`。

> 待本地验证：探测结果取决于机器实际安装的 SDK。

#### 4.3.5 小练习与答案

**练习 1**：如果你新增的 `mygpu` 后端希望优先级**高于** cuda，该改哪里？

**参考答案**：改 [tilelang/\_\_init\_\_.py:211-215](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/__init__.py#L211-L215) 的 import 顺序——把 `from . import mygpu` 放到 `from . import cuda` **之前**。因为 detector 的遍历顺序 = dict 插入顺序 = import 顺序。仅靠 `register_target_detector` 无法插队（它追加在末尾，除非 `override` 覆盖同名项）。

**练习 2**：用户显式写 `target={"kind":"maca"}` 时，会走 detector 吗？

**参考答案**：不会。显式输入走 `_validate_manual_target` → `_normalize_registered_target`（[target.py:85-109](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/backend/target.py#L85-L109)），即走 **normalizer** 而非 detector。detector 只在 `target="auto"` 时被调用。

---

### 4.4 target_utils：C++ 侧的统一能力分发

#### 4.4.1 概念说明

前面三节都是 Python 侧。但 TileLang 的真编译在 C++(`libtilelang.so`)里。C++ 侧同样需要一套「按 target 分发」的机制——比如「这个 target 支不支持异步拷贝？」这类**能力查询**（capability query）。如果每个上层 pass 都自己写 `if cuda ... else if rocm ... else if maca`，代码会很快腐化。

TileLang 的解法是**「公共分发 + 后端自有实现」**：

- 每个后端提供自己的谓词（`TargetIsCuda`/`TargetIsRocm`/`TargetIsMaca`）和实现（`TargetCudaHasAsyncCopy`/...）。
- 公共层 `src/backend/common/target_utils.cc` 只写一个**总入口** `TargetHasAsyncCopy`，按谓词分发到对应后端实现。
- 上层（pass、copy op）只调这一个总入口，并以 FFI 形式暴露为 `tl.TargetHasAsyncCopy`，Python 侧 `target_has_async_copy(target)` 直接调它。

新增后端时，要在 C++ 侧加一个谓词分支，并在公共分发函数里挂上它。

#### 4.4.2 核心流程

```text
Python: target_has_async_copy(target)
        │  tvm.ffi → tl.TargetHasAsyncCopy
        ▼
C++ 公共层: TargetHasAsyncCopy(target)        ← src/backend/common/target_utils.cc
        │
        ├─ if TargetIsCuda(target)  → TargetCudaHasAsyncCopy(target)
        ├─ if TargetIsRocm(target)  → TargetRocmHasAsyncCopy(target)
        ├─ if TargetIsMaca(target)  → TargetMacaHasAsyncCopy(target)   ← MACA 分支
        └─ else → false
```

`TargetIsMaca` 的实现极其简单——比较设备的 `DLDeviceType` 是否等于 `kDLMACA`（[src/maca/target_utils.cc:31-33](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/target_utils.cc#L31-L33)）。这依赖本仓库定制的 TVM 在 `DLDeviceType` 枚举里新增了 `kDLMACA` 值。

#### 4.4.3 源码精读

**(1) 公共分发总入口**。[src/backend/common/target_utils.cc:15-33](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/backend/common/target_utils.cc#L15-L33)：

```cpp
bool TargetHasAsyncCopy(Target target) {
  if (TargetIsCuda(target))  return TargetCudaHasAsyncCopy(target);
  if (TargetIsRocm(target))  return TargetRocmHasAsyncCopy(target);
  if (TargetIsMaca(target))  return TargetMacaHasAsyncCopy(target);   // ← MACA 分支
  return false;
}

TVM_FFI_STATIC_INIT_BLOCK() {
  refl::GlobalDef().def("tl.TargetHasAsyncCopy",
    [](Target target) { return TargetHasAsyncCopy(target); });
}
```

**加 `mygpu` 就是这里加一行 `if (TargetIsMygpu(target)) return TargetMygpuHasAsyncCopy(target);`**。注意头文件 [src/backend/common/target_utils.h:14-18](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/backend/common/target_utils.h#L14-L18) `#include` 了每个后端的 `target_utils.h`，所以公共层能看见所有后端的谓词。

**(2) 后端自有谓词与 FFI 注册**。[src/maca/target_utils.cc:31-44](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/target_utils.cc#L31-L44) 与 [src/maca/target_utils.cc:150-157](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/target_utils.cc#L150-L157)：

```cpp
bool TargetIsMaca(Target target) {
  return target->GetTargetDeviceType() == kDLMACA;   // ← 比设备类型枚举
}
bool TargetMacaHasAsyncCopy(Target target) { return TargetIsMaca(target); }
int  TargetMacaGetWarpSize(Target target)  { (void)target; return 64; }   // ← warp_size=64 钉死

TVM_FFI_STATIC_INIT_BLOCK() {
  refl::GlobalDef()
    .def("tl.TargetIsMaca",        [](Target t){ return TargetIsMaca(t); })
    .def("tl.TargetMacaGetWarpSize",[](Target t){ return TargetMacaGetWarpSize(t); });
}
```

`TVM_FFI_STATIC_INIT_BLOCK` 是「在 `libtilelang.so` 被加载时自动执行」的静态初始化块，用它把 C++ 函数注册成全局 FFI。Python 侧 `tilelang/maca/target.py:40-45` 的 `target_is_maca` / `target_has_async_copy` 正是反向调用这些 FFI——又一次「两边名字对齐」。

**(3) 与 target_kind 的关系**。`TargetIsMaca` 用 `GetTargetDeviceType()` 判断，而设备类型来自 [src/maca/runtime/maca_target_kind.cc:59-71](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/runtime/maca_target_kind.cc#L59-L71) 的 `TVM_REGISTER_TARGET_KIND("maca", kDLMACA)`——第二个参数 `kDLMACA` 就是绑定。也就是说，**target_kind 注册时把 `maca` 这个 kind 绑到了 `kDLMACA` 设备类型，`TargetIsMaca` 才能据此认出它**。

同一处的 canonicalizer `UpdateMACAAttrs`（[maca_target_kind.cc:38-57](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/runtime/maca_target_kind.cc#L38-L57)）负责在 target 构造时自动补齐 `mtriple`（`mxc-metax-macahca`）与 `mcpu`（探测设备架构，兜底 `xcore1000`）。注意 `thread_warp_size` 默认值钉为 **64**（[L67](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/runtime/maca_target_kind.cc#L67)），这是 MACA 与 CUDA（32）最显眼的差异，会牵动 GEMM 的 warp 划分。

#### 4.4.4 代码实践

**实践目标**：跟踪一次「公共分发」的完整调用链。

**操作步骤**：

1. 从 Python 入口 `tilelang.maca.target.target_has_async_copy`（[target.py:44-45](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/target.py#L44-L45)）出发，写出它调的 FFI 名字。
2. 在 C++ 侧找到该 FFI 的注册点（[target_utils.cc:30-32](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/backend/common/target_utils.cc#L30-L32)），再追到 `TargetHasAsyncCopy` 的 MACA 分支，最后到 `TargetMacaHasAsyncCopy`。
3. 在一张纸上画出「Python 函数 → FFI 名 → C++ GlobalDef → 公共分发 → 后端实现」5 个节点。

**需要观察的现象 / 预期结果**：能复述这条链路；并指出「公共分发函数是唯一需要随新后端改动的 C++ 文件」。

> 待本地验证：若有 MetaX 设备，可构造 `Target("maca")` 后实际调用 `target_has_async_copy` 观察返回 `True`。

#### 4.4.5 小练习与答案

**练习 1**：`TargetIsMaca` 不比较 target 的 `kind.name == "maca"`，而是比较 `GetTargetDeviceType() == kDLMACA`。这样做有什么好处？

**参考答案**：解耦「kind 字符串名」与「设备类型」。即便将来出现别名 kind（指向同一设备类型），谓词仍能正确识别；同时 `GetTargetDeviceType()` 是 O(1) 的整数比较，比字符串比较更快。

**练习 2**：假设 `mygpu` 不支持异步拷贝。在公共分发里该怎么挂它的分支？

**参考答案**：在 [target_utils.cc:15-26](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/backend/common/target_utils.cc#L15-L26) 的 `TargetHasAsyncCopy` 里加 `if (TargetIsMygpu(target)) return false;`（或 `TargetMygpuHasAsyncCopy(target)`）。同时别忘了在 `target_utils.h` 里 `#include "mygpu/target_utils.h"`。

---

## 5. 综合实践

> 这是本讲的主任务（即规格中指定的 practice_task）。**以 MACA 为模板，列出新增一个假想后端 `mygpu` 需要修改/新增的 Python 与 C++ 文件清单**。

### 实践目标

把第 4 节的四个最小模块（backend 注册 / lazy 注册 / target detector / target_utils）整合成一份**可执行的「加后端」施工图**，做到看见清单就知道每个文件要填什么。

### 操作步骤

请仿照 MACA，按下面六张表逐项列出 `mygpu` 的改动。每项都要写明：**文件路径（新增 N / 修改 M）+ 关键登记语句 + 对齐的 FFI 名字**。

**表 A — Python 侧：注册中枢（修改 1 个文件）**

| 文件 | 改动 | 参照 MACA |
| --- | --- | --- |
| `tilelang/backend/__init__.py` | M：加 `register_lazy_device_codegen("mygpu", "tilelang.mygpu.codegen")` 与 `register_lazy_execution_backends("mygpu", "tilelang.mygpu.execution_backend")` | [L44-L50](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/backend/__init__.py#L44-L50) |

**表 B — Python 侧：后端包（新增 `tilelang/mygpu/` 目录）**

| 文件 | 作用 | 参照 MACA |
| --- | --- | --- |
| `tilelang/mygpu/__init__.py` | N：import 各子模块触发注册 | [maca/__init__.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/__init__.py#L1-L6) |
| `tilelang/mygpu/target.py` | N：`register_target_detector("mygpu", _detect_mygpu_target, override=True)` + 可用性检测 | [maca/target.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/target.py#L29-L48) |
| `tilelang/mygpu/codegen.py` | N：`register_device_codegen("mygpu", DeviceCodegen(build=global_func_device_codegen("target.build.tilelang_mygpu"), ...))` | [maca/codegen.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/codegen.py#L12-L21) |
| `tilelang/mygpu/execution_backend.py` | N：登记执行后端（至少 `tvm_ffi`） | [maca/execution_backend.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/execution_backend.py#L34-L58) |
| `tilelang/mygpu/pipeline.py` | N：`register_pipeline(PassPipeline("mygpu", MyGPUPassPipelineBody))` | [maca/pipeline.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/pipeline.py#L151-L153) |
| `tilelang/mygpu/intrinsics/`、`op/`、`transform/` | N：按需提供算子/intrinsic/专属 pass（可后续逐步补） | maca 对应子目录 |

**表 C — Python 侧：顶层挂载（修改 1 个文件）**

| 文件 | 改动 | 参照 MACA |
| --- | --- | --- |
| `tilelang/__init__.py` | M：加 `from . import mygpu as mygpu`（位置决定 auto 探测优先级） | [L215](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/__init__.py#L215) |

**表 D — C++ 侧：后端源码（新增 `src/mygpu/` 目录）**

| 文件 | 作用 / 关键登记 | 参照 MACA |
| --- | --- | --- |
| `src/mygpu/runtime/mygpu_target_kind.cc` | N：`TVM_REGISTER_TARGET_KIND("mygpu", kDLMyGPU).add_attr_option<...>(...).set_default_keys({"mygpu","gpu"}).set_target_canonicalizer(UpdateMyGPUAttrs)` | [maca_target_kind.cc:59-71](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/runtime/maca_target_kind.cc#L59-L71) |
| `src/mygpu/target_utils.cc` / `.h` | N：`TargetIsMygpu`、`TargetMygpuGetWarpSize`，并 `TVM_FFI_STATIC_INIT_BLOCK` 注册 `tl.TargetIsMygpu` 等 | [maca/target_utils.cc:31-44,150-157](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/target_utils.cc#L31-L44) |
| `src/mygpu/codegen/codegen_mygpu.cc` / `.h` | N：`CodeGenTileLangMyGPU : public CodeGenC` | [codegen_maca](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/codegen_maca.cc) |
| `src/mygpu/codegen/rt_mod_mygpu.cc` | N：`BuildTileLangMyGPU` + `refl::GlobalDef().def("target.build.tilelang_mygpu", BuildTileLangMyGPU)` | [rt_mod_maca.cc:101-177](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/rt_mod_maca.cc#L101-L177) |
| `src/mygpu/codegen/intrin_rule_mygpu.cc` | N：`<target>.FLowerIntrinsic` 规则 | [intrin_rule_maca.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/intrin_rule_maca.cc) |
| `src/mygpu/runtime/mygpu_device_api.cc` | N：`MyGPUDeviceAPI` + 注册 `device_api.mygpu` | [maca_device_api.cc:43,269-282](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/runtime/maca_device_api.cc#L269-L282) |
| `src/mygpu/runtime/mygpu_module.cc` | N：`MyGPUModuleNode` + `MyGPUModuleCreate` + `ffi.Module.load_from_bytes.mygpu` | [maca_module.cc:289-311](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/runtime/maca_module.cc#L289-L311) |
| `src/mygpu/op/*.cc`、`transform/*.cc` | N：按需补算子与专属 pass | maca 对应目录 |

**表 E — C++ 侧：公共分发（修改 2 个文件）**

| 文件 | 改动 | 参照 MACA |
| --- | --- | --- |
| `src/backend/common/target_utils.h` | M：`#include "mygpu/target_utils.h"` | [L16](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/backend/common/target_utils.h#L14-L18) |
| `src/backend/common/target_utils.cc` | M：在 `TargetHasAsyncCopy` 加 `if (TargetIsMygpu(target)) return TargetMygpuHasAsyncCopy(target);` | [L15-L26](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/backend/common/target_utils.cc#L15-L26) |

**表 F — C++ 侧：构建系统（修改 2 个文件 + 新增 1 个）**

| 文件 | 改动 | 参照 MACA |
| --- | --- | --- |
| `CMakeLists.txt` | M：加 `USE_MYGPU` 环境变量读取块 + `include("${CMAKE_CURRENT_SOURCE_DIR}/src/mygpu/CMakeLists.txt")` | [L416-L422, L465](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/CMakeLists.txt#L416-L422) |
| `src/mygpu/CMakeLists.txt` | N：定义 `TILE_LANG_MYGPU_ALWAYS_SRCS`（target_utils/intrin_rule/lower_intrin 等常驻）与 `TILE_LANG_MYGPU_SRCS`（target_kind/device_api/module/rt_mod 等 `if (NOT USE_MYGPU) return()` 门控） | [src/maca/CMakeLists.txt:2-28](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/CMakeLists.txt#L2-L28) |
| `cmake/FindMYGPU.cmake` | N：仿 `FindMACA.cmake` 定位 SDK（显式路径 → `MYGPU_PATH` → `/opt/mygpu`） | `cmake/FindMACA.cmake` |

### 需要观察的现象 / 预期结果

- 完成表 A–C 后，`import tilelang` 应能让 `list_target_detectors()` 含 `mygpu`、且 `determine_target("auto")` 在合适机器上选中它。
- 完成表 D–E 后，C++ 侧能识别 `Target("mygpu")`（target_kind 注册成功），`tl.TargetIsMygpu` FFI 可调。
- 完成表 F 的门控后，即便 `USE_MYGPU=OFF` 也能正常编译（常驻源码不依赖 SDK），只有 `USE_MYGPU=ON` 才编出真后端。

> ⚠️ 这是「文件清单 + 施工图」型实践，**不要求你真写出全部代码**（那需要真实硬件 SDK）。重点是把「加后端 = 填两张表的具名插槽」这张心智图刻进脑子。若要在无设备机器上验证，最小可验证里程碑是：只做表 D 的 target_kind + target_utils、表 E 的分发、表 F 的常驻源码，`USE_MYGPU=OFF` 下 `import tilelang` 不报错。

## 6. 本讲小结

- **加后端 = 填注册表**：Python 侧填 detector / lazy-codegen / lazy-execution / pipeline 四张表，C++ 侧填 target_kind / device_api / build 函数 / target_utils 四件套，两边用**同名 FFI 字符串**对齐。
- **懒加载**让 `import tilelang` 保持轻量：`register_lazy_*` 只存模块路径字符串，首次用到该 target 才 `import_module` 触发真正的 `register_device_codegen` 等登记，靠 `_LOADED` 集合去重。
- **target detector** 按 Python dict 插入顺序短路遍历，顺序由 `tilelang/__init__.py` 的 import 顺序决定（cuda→hip→metal→maca）；MACA 探测器在「HIP 版 torch」与「找不到 SDK」时主动让位返回 `None`。
- **target_utils 公共分发**是 C++ 侧的统一抽象：上层只调 `tl.TargetHasAsyncCopy` 一个 FFI，公共层按 `TargetIsCuda/Rocm/Maca` 分发到各后端实现；新增后端在此加一个分支即可。
- **MACA 是最佳样板**：它把以上每一步都做到了，照它的文件结构（`tilelang/maca/*` 与 `src/maca/*`）逐文件对照即可推导出 `mygpu` 的施工图。
- **构建系统两段式**：顶层 `CMakeLists.txt` 用 `USE_MACA` 环境变量选后端（注意 MACA 走 `USE_MACA` 而非 `TILELANG_BACKENDS`），`src/maca/CMakeLists.txt` 用 `ALWAYS_SRCS` / `MACA_SRCS` 把「常驻 helper」与「依赖 SDK 的真后端」分层编译。

## 7. 下一步学习建议

- **继续扩展线**：学 **u9-l2（扩展：新增 tile 算子）**，看 `TileOperatorNode` 的 `Lower()` / `InferLayout()` 两个虚方法——它是「在已注册后端里加一个新算子」的对称主题，与本讲的「加一个新后端」配套。
- **深入 MACA 后端细节**：若你想让 `mygpu` 也能跑张量核，回看 **u7-l2（MACA codegen）** 与 **u7-l3（mfma intrinsics）**，了解 `CodeGenTileLangMyGPU` 该 override 哪些 visitor、指令选择 `SelectInst` 该返回什么键。
- **编译流水线衔接**：**u7-l4（MACA 编译流水线）** 讲了 `register_pipeline` 之后引擎如何用 `resolve_pipeline(target)` 按 kind 名分派——本讲只点了注册，那里讲清了「注册的流水线如何被调用」。
- **阅读建议**：对照本讲清单，把 MACA 的 [src/maca/CMakeLists.txt](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/CMakeLists.txt#L2-L28) 与 [tilelang/backend/__init__.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/backend/__init__.py#L36-L50) 从头到尾读一遍，你会获得「加后端」最扎实的肌肉记忆。
