# target 体系与配置

## 1. 本讲目标

在 u1-l4 中你已经跑通了第一个 GEMM kernel，但当时我们几乎没提一个关键问题：**编译器怎么知道要把 kernel 编译给谁？** 是编给 NVIDIA GPU、AMD GPU、MetaX GPU，还是 CPU？同一个 kernel 描述，落到不同硬件上需要走完全不同的代码生成与指令选择。负责回答「编给谁」这个问题的对象，就叫 **target**。

本讲学完后你应当能够：

- 说清楚 TileLang 里 target 是什么，它和 TVM 的 target 是什么关系。
- 区分常见的 target kind（`cuda` / `hip` / `maca` / `metal` / `llvm` / `webgpu` / `c` / `auto`）。
- 用三种输入形式（字符串、配置字典、TVM `Target` 对象）正确地描述一个 target，并知道何时该用哪种。
- 说清楚 `auto` 是按什么顺序、靠什么机制去自动探测硬件的。
- 用环境变量 `TILELANG_DEFAULT_TARGET` 设置默认 target，并理解它与「显式传参」的优先级关系。

本讲覆盖四个最小模块：**target kind、配置字典、auto 检测、默认 target**。

## 2. 前置知识

- **TVM 与 target**：TileLang 构建在 TVM 之上（见 u1-l1）。TVM 用一个叫 `target` 的对象来描述「目标设备」，它决定走哪个代码生成器（CUDA / HIP / Metal / LLVM …），并携带设备相关选项（如 GPU 架构号）。TileLang 直接复用了 TVM 的 `tvm.target.Target`，并在它之上包了一层自己的「解析与注册」逻辑。本讲讲的就是这层包装。
- **target kind**：target 里最核心的一个字段叫 kind，就是一个字符串，比如 `"cuda"`、`"hip"`、`"maca"`。它对应一种「目标平台类型」。
- **target 属性（attrs）**：除 kind 外，target 还可以带若干属性，比如 CUDA 的 `arch`（SM 架构号）、HIP 的 `mcpu`（GPU 型号）、LLVM 的 `mtriple`（CPU 三元组）。这些属性会影响代码生成的细节。
- **本 fork 的差异**：tilelang-metax 相比上游 tilelang 新增了 MetaX GPU 的 **MACA** 后端（见 u1-l1、u1-l3）。在本讲你会看到，`maca` 被注册成了一个和 `cuda`/`hip`/`metal` 平级的一等 target kind，并且会参与 `auto` 自动探测。这正是 metax 分支在 target 层面带来的核心变化。

> 名字小贴士：AMD GPU 的 target kind 叫 `hip`，但对应的 Python 包/目录叫 `rocm`（ROCm 是平台名，HIP 是其编程模型）。所以「包名」和「target 名」未必一致，这在 u1-l3 已提到过。同理，`maca` 这个 target kind 对应 `tilelang/maca/` 目录。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [docs/get_started/targets.md](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/get_started/targets.md) | 官方 target 使用文档，列出常见 target、输入形式、默认 target 与 CUDA `arch`/`code` 用法。 |
| [tilelang/backend/target.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/backend/target.py) | target 解析核心：定义 `determine_target`，以及 detector（探测器）与 normalizer（归一化器）两张注册表。 |
| [tilelang/env.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/env.py) | 读取并解析 `TILELANG_DEFAULT_TARGET` 环境变量（含 JSON 字符串解析）。 |
| [tilelang/cache/\_\_init\_\_.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cache/__init__.py) | 编译入口 `cached` / `_resolve_cache_dispatch`：把「未指定 target」翻译成「读默认 target」再交给 `determine_target`。 |
| [tilelang/cuda/target.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/target.py) | CUDA 的 detector 与 normalizer：探测 CUDA 可用性与 SM 架构，给裸字符串 `"cuda"` 自动补 `arch`。 |
| [tilelang/maca/target.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/target.py) | MACA 的 detector：探测 MACA SDK 可用性，注册成 `auto` 探测链的一环。 |
| [tilelang/\_\_init\_\_.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/__init__.py) | 在 `import tilelang` 时按固定顺序导入各后端，从而决定 detector 的注册顺序（即 `auto` 的探测顺序）。 |
| [testing/python/target/test_tilelang_target.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/testing/python/target/test_tilelang_target.py) | target 子系统的单元测试，是理解各种输入行为的最可靠依据。 |

---

## 4. 核心概念与源码讲解

### 4.1 target 是什么：target kind 与三种输入形式

#### 4.1.1 概念说明

一个 target 回答两个问题：**「编给哪类设备」**（kind）和 **「这台设备的具体参数是什么」**（attrs）。比如：

- `{"kind": "cuda", "arch": "sm_90"}` 表示「编给 NVIDIA GPU，架构是 SM 9.0（Hopper）」。
- `{"kind": "hip", "mcpu": "gfx90a"}` 表示「编给 AMD GPU，型号 gfx90a（MI200 系列）」。
- `{"kind": "llvm", "mtriple": "x86_64-linux-gnu"}` 表示「编给 x86_64 Linux CPU」。

TileLang 允许你用**三种等价的输入形式**来表达同一个 target，这是它最贴心的设计之一（见文档 [docs/get_started/targets.md:35-49](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/get_started/targets.md#L35-L49)）：

```python
target = "cuda"                              # ① 裸字符串：只有 kind，最简单
target = {"kind": "cuda", "arch": "sm_90"}   # ② 配置字典：kind + 属性
target = tvm.target.Target({"kind": "cuda"}) # ③ 已经构造好的 TVM Target 对象
```

常见 target kind 一览（来自文档 [docs/get_started/targets.md:13-22](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/get_started/targets.md#L13-L22)）：

| kind | 说明 |
| ---- | ---- |
| `auto` | 自动探测，依次尝试 CUDA → HIP → Metal（metax 分支还追加 MACA）。 |
| `cuda` | NVIDIA GPU。可用字典带 `arch`，如 `{"kind": "cuda", "arch": "sm_80"}`。 |
| `cutedsl` | NVIDIA CUTLASS/CuTe DSL 后端，需安装 `nvidia-cutlass-dsl`。 |
| `hip` | AMD GPU（ROCm）。可用字典带 `mcpu`，如 `{"kind": "hip", "mcpu": "gfx90a"}`。 |
| `metal` | Apple Silicon GPU（arm64 Mac）。 |
| `llvm` | CPU 执行。可用字典带 `mtriple` 等。 |
| `webgpu` | 浏览器 / WebGPU 运行时。 |
| `c` | 输出纯 C 源码，用于检查或自定义工具链。 |
| `maca` | **MetaX GPU**（本 fork 新增），与 `cuda`/`hip` 平级。 |

> 注意：上面这张表是上游文档原样搬运，**没有列出 `maca`**。但在 metax 分支里，`maca` 已经被注册成一个真实可用的 target kind（既能手动传 `{"kind": "maca"}`，也会被 `auto` 探测到）。这是文档尚未同步的一处细节，读源码时以代码为准。

#### 4.1.2 核心流程

无论你用哪种输入形式，最终都要汇入同一个「解析总入口」 `determine_target`。它的决策逻辑可以用下面这段伪代码概括：

```
determine_target(target, return_object):
    如果 target == "auto":
        # 「请帮我找一个」
        优先用 TVM 当前上下文里的 Target（Target.current()）
        否则调用 auto_detect_target() 逐个问探测器
    否则:
        # 「我指定了，请帮我校验/归一化」
        先过一遍 normalizer（如把裸 "cuda" 补上 arch）
        再校验能否被 tvm.target.Target 构造成功
    最后按需包成 TVM Target 对象返回
```

整条链路的层级关系是：

```
你的代码  tilelang.compile(target=...) / @tilelang.jit(target=...)
   │
   ▼
tilelang.cache.cached()  →  _resolve_cache_dispatch()
   │   (target=None 在这里被翻译成「读默认 target」)
   ▼
tilelang.backend.target.determine_target()   ← 本讲的「总入口」
   │   （auto 检测 / 手动校验 / normalizer 归一化）
   ▼
一个确定的 tvm.target.Target  →  交给代码生成器（codegen）与执行后端
```

#### 4.1.3 源码精读

先看输入形式的类型定义。[tilelang/backend/target.py:9-13](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/backend/target.py#L9-L13) 定义了三种允许的输入：

```python
TargetConfig = dict[str, object]
TargetInput = str | Mapping[str, object] | Target
TargetLike = str | TargetConfig | Target
```

即 `str`（裸字符串）、`dict`（配置字典）、`Target`（TVM 对象）三选一。

再看总入口 [`determine_target`](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/backend/target.py#L122-L134)，它把 `auto` 分支和手动分支分开处理：

```python
def determine_target(target="auto", return_object=False):
    if target == "auto":
        current_target = Target.current(allow_none=True)
        return_var = current_target if current_target is not None else auto_detect_target()
    else:
        return_var = _validate_manual_target(target)
    return _finalize_target(return_var, return_object=return_object)
```

这段代码做了三件事：

1. 如果传的是 `"auto"`，优先尊重 TVM 的上下文目标 `Target.current()`（这样可以配合 `with tvm.target.Target(...):` 使用），找不到才走自动探测。
2. 否则进入 `_validate_manual_target` 做手动校验。
3. 最后 `_finalize_target` 决定是返回「规范化后的字符串/字典」还是返回一个 `Target` 对象（由 `return_object` 控制）。

手动校验的核心在 [`_validate_manual_target`](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/backend/target.py#L85-L109)，它对三种输入分别处理：

```python
def _validate_manual_target(target):
    normalized = _normalize_registered_target(target)   # 先试 normalizer
    if normalized is not None:
        return normalized
    if isinstance(target, Target):   # ③ Target 对象：直接用
        return target
    if isinstance(target, dict):     # ② 字典：试着用 TVM 构造，失败给友好报错
        try: Target(target)
        except Exception as err:
            raise AssertionError("...Pass a valid target config dict.") from err
        return target
    if isinstance(target, str):      # ① 字符串：strip 后试构造
        ...
```

关键点：**无论哪种形式，最终都必须能被 `tvm.target.Target(...)` 成功构造**，否则会抛出带提示的 `AssertionError`。这就是为什么传错属性（比如把 CUDA 的 `code` 写成字符串）会被拒绝——因为 TVM 构造时就拒绝了。

#### 4.1.4 代码实践

**实践目标**：亲手用三种输入形式调用 `determine_target`，确认它们都能被解析。

**操作步骤**（在能 `import tilelang` 的环境里运行；**无 GPU 也可**，因为下面只做解析、不真正编译）：

```python
# 示例代码：练习 4.1
import tilelang
from tvm.target import Target
from tilelang.backend.target import determine_target

# ① 裸字符串（无 CUDA 时会被 normalizer 原样保留为 "cuda"）
t1 = determine_target("cuda", return_object=True)

# ② 配置字典
t2 = determine_target({"kind": "llvm"}, return_object=True)

# ③ 已经构造好的 TVM Target 对象
t3 = determine_target(Target({"kind": "metal"}), return_object=True)

print(type(t1).__name__, t1)   # 预期: Target，含 cuda
print(type(t2).__name__, t2)   # 预期: Target，含 llvm
print(type(t3).__name__, t3)   # 预期: Target，含 metal
```

**需要观察的现象**：三种形式返回的都是 `tvm.target.Target` 对象（因为我们传了 `return_object=True`），且 `.kind.name` 分别是 `cuda` / `llvm` / `metal`。

**预期结果**：三行打印都是 `Target ...`，没有抛异常。

**何时用哪种形式**（对应本讲代码实践任务）：

| 形式 | 适用场景 |
| ---- | -------- |
| 裸字符串 `"cuda"` | 只需指定平台、不需要任何属性时最简洁；快速原型、跨机器脚本用 `"auto"`。 |
| 配置字典 `{"kind":..., "arch":...}` | 需要设备属性（CUDA `arch`/`code`、HIP `mcpu`、LLVM `mtriple`）时**首选**，比 CLI 风格字符串更清晰、可读。 |
| TVM `Target` 对象 | 需要复用同一个 target、或要带 `host` 目标（交叉编译）时；以及从别处已经拿到 `Target` 时。 |

#### 4.1.5 小练习与答案

**练习 1**：`determine_target("auto")` 在没有任何 GPU 的纯 CPU 机器上会发生什么？

**参考答案**：它会进入 `auto_detect_target()`，依次询问 CUDA / HIP / Metal / MACA 探测器。这些探测器在无对应硬件时都返回 `None`。如果全部返回 `None`，[`auto_detect_target`](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/backend/target.py#L66-L78) 会抛 `ValueError("No registered target detector found an available target.")`，并把试过的探测器和报错一起打印出来。

**练习 2**：为什么文档说「有属性的 target 优先用字典，而不是 CLI 风格字符串」？

**参考答案**：因为字典形式可读、结构化，且能表达列表等复杂属性（如 CUDA 的 `code: ["sm_100a","sm_103a"]`）；而 CLI 风格字符串容易写错、且 TileLang 对某些形式（如逗号分隔的 `code`）**故意不支持**。字典还能让 TVM 在构造时给出精确的属性校验错误。

---

### 4.2 配置字典：用属性精确定位硬件

#### 4.2.1 概念说明

裸字符串只能表达 kind，表达不了「具体哪一代硬件」。当代码生成需要做架构相关的决策时（比如 Hopper 才有 WGMMA、 Ampere 才有异步拷贝、MetaX 的 MFMA 指令也有特定形状），就需要用**配置字典**带上属性。

最典型的属性：

- CUDA：`arch`（SM 架构号，如 `sm_80`、`sm_90a`）、`code`（可选，多个 SASS code 的列表）。
- HIP：`mcpu`（GPU 型号，如 `gfx90a`）。
- LLVM：`mtriple`（CPU 三元组，如 `x86_64-linux-gnu`）、`mcpu`（CPU 型号）。

#### 4.2.2 核心流程

当你传一个字典时，流程是：

```
你的字典 {"kind": "cuda", "arch": "sm_90"}
   │
   ▼  _validate_manual_target
先过 normalizer（CUDA normalizer 只认裸字符串 "cuda"，对字典返回 None，放行）
   │
   ▼  Target(字典)  ← TVM 构造校验
成功 → 返回字典；失败 → AssertionError("...valid target config dict...")
   │
   ▼  _finalize_target(return_object=True)
包成 tvm.target.Target 对象
```

也就是说，**字典的合法性最终由 TVM 的 target 构造器裁决**。任何该 kind 不认识的属性、或取值非法的属性，都会在 `Target(字典)` 这一步被拒绝。

#### 4.2.3 源码精读

CUDA 的 `arch` 和 `code` 用法在文档 [docs/get_started/targets.md:76-108](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/get_started/targets.md#L76-L108) 有详细说明。两个要点：

1. 通常只设 `arch` 就够，TileLang 会把它转成 NVCC 的 `-arch=sm_90`。
2. 只有在需要显式 `-gencode` 行为（一个虚拟架构派生、但为多个 GPU code 发射 SASS）时才用 `code`，且 **`code` 必须是列表**：

```python
target = {
    "kind": "cuda",
    "arch": "sm_100f",
    "code": ["sm_100a", "sm_103a"],   # 必须是 list，不能写成 "sm_100a,sm_103a"
}
```

这点在测试里有明确的行为断言。[testing/python/target/test_tilelang_target.py:74-81](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/testing/python/target/test_tilelang_target.py#L74-L81) 验证了「字符串 code」和「`compute_` 前缀的 arch」都会被拒绝：

```python
def test_cuda_target_code_attr_rejects_string_code():
    with pytest.raises(AssertionError, match="valid target config dict"):
        determine_target({"kind": "cuda", "arch": "sm_100f", "code": "sm_100a"}, ...)

def test_cuda_target_rejects_compute_arch():
    with pytest.raises(AssertionError, match="valid target config dict"):
        determine_target({"kind": "cuda", "arch": "compute_90"}, ...)
```

而合法的列表 `code` 能完整穿过归一化、保留到最终的 `Target` 对象上，见 [test_tilelang_target.py:61-71](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/testing/python/target/test_tilelang_target.py#L61-L71)。

#### 4.2.4 代码实践

**实践目标**：体会「字典属性必须合法」这一约束。

**操作步骤**：

```python
# 示例代码：练习 4.2
from tilelang.backend.target import determine_target

# (a) 合法：列表形式的 code
t = determine_target(
    {"kind": "cuda", "arch": "sm_100f", "code": ["sm_100a", "sm_103a"]},
    return_object=True,
)
print("arch =", t.attrs["arch"], " code =", list(t.attrs["code"]))

# (b) 非法：code 写成字符串 —— 观察报错
try:
    determine_target({"kind": "cuda", "arch": "sm_100f", "code": "sm_100a"},
                     return_object=True)
except AssertionError as e:
    print("被拒绝，符合预期：", str(e)[:60], "...")
```

**需要观察的现象**：(a) 成功打印 `arch = sm_100f  code = ['sm_100a', 'sm_103a']`；(b) 抛出 `AssertionError`，提示信息里含「valid target config dict」。

**预期结果**：与上述一致。若你的环境里 TVM 版本对 SM token 校验更严，以实际报错为准——核心是「非法字典会被 `Target(...)` 构造阶段拒绝」。如不确定本地 TVM 行为，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：如果你想给 AMD MI200 编译，target 字典该怎么写？

**参考答案**：`{"kind": "hip", "mcpu": "gfx90a"}`。`mcpu` 指定 GPU 型号，由 ROCm codegen 用于选择对应的指令集。

**练习 2**：`code` 含多个条目时，TileLang 为什么会发出 fat binary（fatbin）？

**参考答案**：因为 NVCC 不允许 `--cubin` 输出同时包含多个 GPU code 实例；当 `code` 有多个条目时，只能以 fat binary 形式打包多个 SASS。见文档 [docs/get_started/targets.md:110-111](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/get_started/targets.md#L110-L111)。

---

### 4.3 auto 检测：注册表与探测顺序

#### 4.3.1 概念说明

当你不想（或不能）写死 target 时，传 `"auto"`，TileLang 会**自动探测当前机器上可用的硬件**。这套机制由两张「注册表」驱动，理解它们是理解整个 target 子系统的钥匙：

- **探测器（detector）**：回答「这台机器上有没有可用的 X 后端」。只在 `auto` 模式下被调用。每个后端（cuda/hip/metal/maca）注册一个。
- **归一化器（normalizer）**：回答「这个用户给定的 target 要不要被改写」。在手动模式和最终收尾时都会被调用（比如把裸 `"cuda"` 改写成带 `arch` 的版本）。

这两者都是「注册—遍历」模式：维护一个字典，按**插入顺序**逐个调用，第一个返回非 `None` 的胜出。

#### 4.3.2 核心流程

`auto` 检测的流程：

```
determine_target("auto")
   │  （先看 Target.current()，没有才 ↓）
   ▼
auto_detect_target():
   按 _TARGET_DETECTORS 的插入顺序，依次调用每个 detector:
      cuda detector → 找到可用 CUDA? 返回 Target；否则 None
      hip  detector → 找到可用 ROCm? 返回 Target；否则 None
      metal detector → ...
      maca detector → 找到可用 MACA? 返回 Target；否则 None
   第一个非 None 的就是结果；全 None → 抛 ValueError
```

**探测顺序就是 detector 的注册顺序**，而注册顺序由 `import tilelang` 时导入各后端的顺序决定。

那么 detector 的注册顺序从哪来？看 [tilelang/\_\_init\_\_.py:211-215](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/__init__.py#L211-L215)：

```python
from . import cpu as cpu      # 不注册 detector
from . import cuda as cuda    # 注册 "cuda" detector   ← 第 1
from . import rocm as rocm    # 注册 "hip"  detector   ← 第 2
from . import metal as metal  # 注册 "metal" detector  ← 第 3
from . import maca as maca    # 注册 "maca" detector   ← 第 4（metax 新增）
```

所以 metax 分支里 `auto` 的真实探测顺序是：**CUDA → HIP → Metal → MACA**。（上游文档只写了 CUDA → HIP → Metal，因为上游没有 MACA。）

#### 4.3.3 源码精读

探测主循环 [`auto_detect_target`](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/backend/target.py#L66-L78)：

```python
def auto_detect_target():
    errors = []
    for spec in _TARGET_DETECTORS.values():   # 按插入顺序遍历
        try:
            detected = spec.detect()
        except Exception as err:
            errors.append(f"{spec.name}: {err}")
            continue
        if detected is not None:
            return detected                    # 第一个非 None 即返回
    details = f" Tried: {', '.join(errors)}." if errors else ""
    raise ValueError(f"No registered target detector found an available target.{details}")
```

两个细节值得注意：

1. **容错**：某个探测器抛异常不会让整个检测崩溃，而是记录下来继续试下一个，最后把所有错误汇总进报错信息——非常便于排查。
2. **短路**：第一个返回非 `None` 的探测器直接胜出，后面的不再调用。这就是「顺序」之所以重要的原因。

注册函数 [`register_target_detector`](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/backend/target.py#L32-L42) 把探测器塞进全局字典 `_TARGET_DETECTORS`（Python dict 自 3.7 起保持插入顺序）：

```python
def register_target_detector(name, detect, *, override=False):
    if name in _TARGET_DETECTORS and not override:
        raise ValueError(f"Target detector {name!r} is already registered")
    _TARGET_DETECTORS[name] = TargetDetectorSpec(name=name, detect=detect)
```

各后端在自己的 `target.py` 末尾调用它注册。CUDA 的探测器 [`_detect_cuda_target`](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/target.py#L48-L57) 先排除「torch 处于 HIP 模式」和「CUDA 不可用」，再探测 SM 架构：

```python
def _detect_cuda_target():
    import torch
    if torch.version.hip is not None:        # ROCm 环境下不抢当 CUDA
        return None
    if not check_cuda_availability():
        return None
    arch = _detect_torch_cuda_arch()         # 从 PyTorch 读 SM 架构
    return _cuda_target_from_arch(arch)
```

MACA 的探测器 [`_detect_maca_target`](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/target.py#L29-L37) 逻辑对称——同样排除 HIP 模式，再通过 `check_maca_availability()` 找 MACA SDK：

```python
def _detect_maca_target():
    import torch
    if torch.version.hip is not None:
        return None
    if not check_maca_availability():        # 找不到 MACA SDK 就返回 None
        return None
    return Target("maca")
```

而 `check_maca_availability` 的实质是去定位 MACA 安装路径（见 u1-l2 提到的 `find_maca_path` 三级搜索），[tilelang/maca/target.py:14-26](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/target.py#L14-L26)。所以「MACA 能否被 auto 探测到」完全取决于本机有没有装好 MACA SDK（`MACA_PATH` / `/opt/maca`）。

> 顺带区分 normalizer：CUDA 还注册了一个 [`normalize_cuda_target`](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/target.py#L60-L64)，它**不参与 auto 探测**，只在手动传裸字符串 `"cuda"` 时把它升级成带 `arch` 的版本。这就是为什么即使你只写 `determine_target("cuda")`，最终拿到的 target 也常常带了真实 SM 架构号。测试 [test_bare_cuda_target_uses_detected_exact_arch](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/testing/python/target/test_tilelang_target.py#L49-L58) 正是验证这一点。

#### 4.3.4 代码实践

**实践目标**：用代码确认「auto 检测 = 按注册顺序遍历探测器、首个非 None 胜出」。

**操作步骤**：阅读测试 [test_auto_target_detector_falls_through_none_result](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/testing/python/target/test_tilelang_target.py#L102-L115)。该测试临时清空探测器表，注册了两个探测器：第一个恒返回 `None`，第二个返回 `"llvm"`，然后断言 `auto_detect_target()` 的结果是 `"llvm"`。你可以照着在自己的 REPL 里复现：

```python
# 示例代码：练习 4.3（理解探测顺序，谨慎操作全局表）
import tilelang.backend.target as tr

# 备份并临时替换探测器表（演示完务必还原！）
old = dict(tr._TARGET_DETECTORS)
try:
    tr._TARGET_DETECTORS.clear()
    tr.register_target_detector("first",  lambda: None,      override=True)
    tr.register_target_detector("second", lambda: "llvm",    override=True)
    print("detectors:", tr.list_target_detectors())   # ('first', 'second')
    print("picked:", tr.auto_detect_target())         # 'llvm'
finally:
    tr._TARGET_DETECTORS.clear()
    tr._TARGET_DETECTORS.update(old)
```

**需要观察的现象**：`list_target_detectors()` 返回元组 `('first', 'second')`，体现插入顺序；`auto_detect_target()` 返回 `'llvm'`，说明第一个返回 `None` 的探测器被「跳过」，第二个胜出。

**预期结果**：如上。演示结束后 `finally` 还原全局表，避免污染后续编译。

#### 4.3.5 小练习与答案

**练习 1**：在一台同时装了 CUDA 和 MACA SDK 的机器上，`auto` 会选哪个？为什么？

**参考答案**：选 CUDA。因为注册顺序是 cuda → hip → metal → maca，CUDA 探测器排在最前且会返回非 `None`，直接短路返回，maca 探测器根本不会被调用。要强制用 MACA，必须显式传 `target={"kind":"maca"}` 或 `"maca"`。

**练习 2**：探测器抛异常和返回 `None`，对 `auto_detect_target` 来说有区别吗？

**参考答案**：结果上都是「跳过这个探测器、继续下一个」。区别在于抛异常时会把 `{name}: {err}` 收集进 `errors` 列表，最后若全部失败，汇总进 `ValueError` 的提示里，方便排查；返回 `None` 则不留信息。见 [tilelang/backend/target.py:66-78](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/backend/target.py#L66-L78)。

---

### 4.4 默认 target：TILELANG_DEFAULT_TARGET

#### 4.4.1 概念说明

如果你调用 `tilelang.compile(...)` 或 `@tilelang.jit` 时**根本不传 `target`**，TileLang 不会报错，而是去读环境变量 `TILELANG_DEFAULT_TARGET`。这个变量没设时，默认值是 `"auto"`。这让同一份脚本可以在不同机器上「零配置」运行。

`TILELANG_DEFAULT_TARGET` 支持两种写法（见文档 [docs/get_started/targets.md:51-66](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/get_started/targets.md#L51-L66)）：

```bash
# 简单：只给 kind
export TILELANG_DEFAULT_TARGET=cuda

# 带属性：给一个 JSON 对象字符串
export TILELANG_DEFAULT_TARGET='{"kind": "cuda", "arch": "sm_90"}'
```

#### 4.4.2 核心流程

「不传 target」到「得到确定 target」的完整链路：

```
tilelang.compile(target=None)
   │
   ▼ tilelang.cache.cached()  →  _resolve_cache_dispatch()
if target is None:
    target = env.get_default_target()       # 读 TILELANG_DEFAULT_TARGET（默认 "auto"）
norm_target = determine_target(target, return_object=True)   # 再走 4.1 的解析
   │
   ▼
确定的 TVM Target → 代码生成
```

注意优先级：**显式传参 > `Target.current()` 上下文 > `TILELANG_DEFAULT_TARGET`（默认 auto）**。具体说：

- 你在 `compile(target=X)` 里显式给了 X，就用 X（先经 normalizer/校验）。
- 你没给（`None`），就读环境变量；环境变量默认 `"auto"`。
- `"auto"` 在 `determine_target` 里又会先看 `Target.current()`，没有才真的去探测硬件。

`get_default_target` 还会把「长得像 JSON 的字符串」解析成字典，规则在 [`_parse_target_config`](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/env.py#L306-L321)：

- 以 `{` 开头才尝试 `json.loads`；否则当成普通字符串原样返回。
- 解析失败（如 `{kind: "cuda"}` 这种没给键加引号的非合法 JSON）会抛 `ValueError`，提示用合法 JSON 语法。

#### 4.4.3 源码精读

环境变量本身的声明在 [tilelang/env.py:381](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/env.py#L381)，默认值 `"auto"`：

```python
TILELANG_DEFAULT_TARGET = EnvVar("TILELANG_DEFAULT_TARGET", "auto")
```

读取与解析在 [`get_default_target`](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/env.py#L453-L462)：

```python
def get_default_target(self):
    target = self.TILELANG_DEFAULT_TARGET
    if target is None:
        return "auto"
    if isinstance(target, Mapping):
        return dict(target)
    if isinstance(target, str):
        return _parse_target_config(target) or target   # 是 JSON 就解析成 dict，否则原样
    raise TypeError("TILELANG_DEFAULT_TARGET must be a string or target config dict")
```

而把 `None` 翻译成「读默认 target」的关键一步，在编译入口 [`_resolve_cache_dispatch`](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cache/__init__.py#L32-L47)（所有编译路径都经过这里）：

```python
def _resolve_cache_dispatch(target, execution_backend, verbose):
    if target is None:
        target = env.get_default_target()                 # ← None → 默认 target
    ...
    norm_target = _determine_target(target, return_object=True)   # ← 再统一解析
    ...
```

这正是文档承诺「`target=None` 时读 `TILELANG_DEFAULT_TARGET`」的落点。测试 [test_default_target_env_accepts_json_string](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/testing/python/target/test_tilelang_target.py#L26-L33) 验证了 JSON 字符串能被正确解析成字典，而 [test_default_target_env_keeps_plain_string](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/testing/python/target/test_tilelang_target.py#L43-L46) 验证了普通字符串原样保留。

#### 4.4.4 代码实践

**实践目标**：通过设置环境变量改变默认 target，并观察解析结果。

**操作步骤**（在 shell 里）：

```bash
# (a) 简单字符串
export TILELANG_DEFAULT_TARGET=cuda
python -c "import tilelang; print(tilelang.env.get_default_target())"
# 预期: cuda

# (b) JSON 字符串（带属性）
export TILELANG_DEFAULT_TARGET='{"kind": "cuda", "arch": "sm_90"}'
python -c "import tilelang; print(tilelang.env.get_default_target())"
# 预期: {'kind': 'cuda', 'arch': 'sm_90'}

# (c) 非法 JSON（键没加引号）
export TILELANG_DEFAULT_TARGET='{kind: "cuda", arch: "sm_90"}'
python -c "import tilelang; print(tilelang.env.get_default_target())"
# 预期: 抛 ValueError，提示 "Use JSON syntax ..."
```

**需要观察的现象**：(a) 返回字符串；(b) 返回字典；(c) 抛 `ValueError`。

**预期结果**：与上述一致。这组用例与仓库测试 [test_tilelang_target.py:26-46](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/testing/python/target/test_tilelang_target.py#L26-L46) 完全对应。

> 提示：在 Python 代码里，**优先直接传字典**（`target={"kind":"cuda","arch":"sm_90"}`）而不是依赖环境变量里的 JSON 字符串——更不容易出错，也更可读。环境变量主要用于「不想改代码、只改运行环境」的场景（如 CI、批量脚本）。

#### 4.4.5 小练习与答案

**练习 1**：既没设 `TILELANG_DEFAULT_TARGET`，又没在 `compile` 里传 `target`，会怎样？

**参考答案**：`get_default_target` 返回默认值 `"auto"`，`determine_target("auto")` 再去自动探测硬件。也就是说等价于 `target="auto"`。

**练习 2**：为什么 `TILELANG_DEFAULT_TARGET` 的 JSON 必须用双引号、且键也要加引号？

**参考答案**：因为它走的是标准库 `json.loads`（见 [`_parse_target_config`](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/env.py#L306-L321)），JSON 标准要求字符串用双引号、键必须是字符串。写成 `{kind: "cuda"}` 不是合法 JSON，会抛 `ValueError` 提示用 JSON 语法。

---

## 5. 综合实践

把四个最小模块串起来，完成下面这个「target 解析小实验」。

**任务**：写一个脚本，分别用**字符串、字典、TVM Target 对象**三种形式调用 `determine_target`，并把它们的解析结果统一打印出来；再额外验证一条「优先级链」。

```python
# 示例代码：综合实践
import os
import tilelang
from tvm.target import Target
from tilelang.backend.target import determine_target

def show(label, value):
    print(f"{label:18} -> kind={value.kind.name:6} attrs={dict(value.attrs)}")

# ① 字符串  ② 字典  ③ Target 对象
show("string  'cuda'", determine_target("cuda", return_object=True))
show("dict    cuda/sm90", determine_target({"kind": "cuda", "arch": "sm_90"}, return_object=True))
show("Target  object", determine_target(Target({"kind": "llvm"}), return_object=True))

# 验证优先级：显式传参 vs 默认 target
os.environ["TILELANG_DEFAULT_TARGET"] = "cuda"
print("default_target =", tilelang.env.get_default_target())   # 'cuda'
# 但显式传 hip 时，默认值被忽略：
show("explicit hip", determine_target({"kind": "hip"}, return_object=True))
```

**你要回答的问题**（对应本讲代码实践任务）：

1. 三种形式解析出的 `kind` 分别是什么？它们的 `.attrs` 有何不同？
2. 何时该用字符串、何时该用字典、何时该用 `Target` 对象？（参考 4.1.4 的表格写出你自己的判断）
3. 当 `TILELANG_DEFAULT_TARGET=cuda` 但你显式传了 `{"kind":"hip"}` 时，最终用的是哪个？为什么？

**参考结论**：

1. kind 分别为 `cuda` / `cuda` / `llvm`；裸字符串 `"cuda"` 经 normalizer 可能带 `arch`（取决于本机是否检测到 CUDA），字典带的属性就是你自己写的，`Target` 对象属性来自构造时。
2. 简单指定平台用字符串；需要属性用字典；需要复用/带 host 目标用 `Target` 对象。
3. 用 `hip`。因为「显式传参 > 默认 target」，`_resolve_cache_dispatch` 只在 `target is None` 时才读默认值（见 [cache/\_\_init\_\_.py:37-38](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cache/__init__.py#L37-L38)）。

> 如果你的机器没有对应 GPU，部分 `determine_target` 仍可成功（它只做解析与校验，不真正编译）；但真正跑 kernel 时还需硬件与驱动到位。无 GPU 环境下请把「实际能否运行 kernel」标注为「待本地验证」。

## 6. 本讲小结

- **target 回答「编给谁」**：由 kind（平台类型）+ attrs（设备属性）组成，TileLang 复用 TVM 的 `tvm.target.Target`。
- **三种输入形式等价**：字符串、配置字典、`Target` 对象，最终都汇入 `determine_target`，且都必须能被 `Target(...)` 成功构造。
- **有属性优先用字典**：如 `{"kind":"cuda","arch":"sm_90"}`、`{"kind":"hip","mcpu":"gfx90a"}`；CUDA 的 `code` 必须是列表。
- **`auto` = 按注册顺序遍历探测器**：metax 分支的真实顺序是 CUDA → HIP → Metal → MACA，首个非 `None` 短路胜出，全失败抛带汇总信息的 `ValueError`。
- **探测器 vs 归一化器**：detector 只在 `auto` 时找硬件；normalizer 在手动输入时改写 target（如给裸 `"cuda"` 补 `arch`）。
- **默认 target**：不传 `target` 时读 `TILELANG_DEFAULT_TARGET`（默认 `"auto"`），支持 JSON 字符串；优先级是「显式传参 > `Target.current()` > 环境变量」。

## 7. 下一步学习建议

- 本讲只解决了「target 怎么表达与解析」。**target 如何真正驱动编译**，请进 u3-l2「JIT 编译与 kernel 对象」，看 `determine_target` 的产物如何流进 `JITKernel` 与各执行后端。
- 想在 **MetaX GPU** 上实际跑 kernel，请进 u3-l3「在 Metax GPU 上运行（MACA target）」，那里会展开 `check_maca_availability` 与 MACA 执行后端的细节。
- 想了解 target 如何影响**指令选择**（如 cuda→wgmma、maca→mfma），可先翻 u4-l2，但建议按顺序学完 u3 再进入编译流水线单元。
- 对「如何新增一个 target 后端」感兴趣的同学，本讲的 detector/normalizer 注册机制是 u9-l1「扩展：新增目标后端」的直接前置知识，届时你会以 MACA 为模板自己注册一个新后端。
