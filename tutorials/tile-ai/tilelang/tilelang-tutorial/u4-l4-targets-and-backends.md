# 目标判定与多后端支持

## 1. 本讲目标

本讲承接 u4-l1（编译总流程），专门回答一个问题：**用户写的一个 `target`，到底是怎么变成 TVM `Target` 并决定「用哪套代码生成、用哪套语言方言」的？**

学完后你应该能够：

1. 说清 `determine_target()` 对 `"auto"`、字符串、字典、`Target` 四种输入的处理路径。
2. 解释 `target="auto"` 为何按 **CUDA → HIP → Metal** 顺序探测，以及这套探测/归一化机制是如何用「注册表」组织起来的。
3. 掌握 `tilelang.language`（默认 CUDA 方言）与 `tilelang.<backend>.language`（rocm/metal/cpu/webgpu）子包的层级关系。
4. 理解 CuTeDSL 不是独立 target，而是「叠加在 `cuda` target 之上的一个后端变体」，并能定位它的判定与分流代码。

## 2. 前置知识

- **TVM Target**：TVM 用一个 `Target` 对象描述「为哪种设备编译」，它包含 `kind`（如 `cuda`/`hip`/`llvm`/`metal`/`c`/`webgpu`）和若干属性（如 CUDA 的 `arch`、HIP 的 `mcpu`）。tilelang 构建在 TVM 之上，因此复用 `tvm.target.Target`。
- **kind 与 keys**：`target.kind.name` 是主类型；`target.keys` 是一组附加标签，CuTeDSL 就靠在 `keys` 里塞一个 `"cutedsl"` 来与普通 CUDA 区分。
- **注册表 + 惰性加载**：tilelang 大量使用「字典当注册表 + 首次使用时 `import_module`」的模式。本讲的探测器、归一化器、device codegen、execution backend 全是这个模式。
- **Python 3.7+ dict 保序**：注册表用普通 `dict`，遍历顺序等于注册顺序，这正是 `auto` 探测顺序的来源。
- 如果你还没读过 u4-l1，建议先了解 `lower()` → `determine_target` → `resolve_pipeline` → `resolve_device_codegen` 的总体位置关系。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tilelang/backend/target.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/target.py) | target 判定的**统一入口**：`determine_target`、探测器/归一化器注册表、`auto_detect_target`。 |
| [tilelang/cuda/target.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/target.py) | CUDA 探测器、CUDA/CuTeDSL 归一化器、SM 架构判断 helper。 |
| [tilelang/rocm/target.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/rocm/target.py) | HIP(ROCm) 探测器与归一化器、warp size / mtriple 补全。 |
| [tilelang/metal/target.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/metal/target.py) | Metal 探测器（仅 arm64 Mac）。 |
| [tilelang/cuda/codegen.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/codegen.py) | 注册「普通 CUDA」与「CuTeDSL」两套 device codegen，用 `supports_target` 谓词分流。 |
| [tilelang/backend/device_codegen.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/device_codegen.py) | device codegen 注册表与惰性加载、`resolve_device_codegen`。 |
| [tilelang/backend/execution_backend.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/execution_backend.py) | 执行后端注册表与 `resolve_execution_backend_spec`（`auto` 推断）。 |
| [tilelang/language/\_\_init\_\_.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/__init__.py) | 默认语言门面：直接再导出 CUDA 方言。 |
| [tilelang/cuda/language/\_\_init\_\_.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/language/__init__.py) | CUDA 方言 = 公共 common 面 + CUDA 扩展（cluster/warpgroup/TMA/intrinsics…）。 |
| [tilelang/rocm/language/\_\_init\_\_.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/rocm/language/__init__.py) | ROCm 方言 = common + ROCm MFMA intrinsics。 |
| [tilelang/cpu/language/\_\_init\_\_.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cpu/language/__init__.py)、[metal](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/metal/language/__init__.py)、[webgpu](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/webgpu/language/__init__.py) | CPU / Metal / WebGPU 三个方言，目前主要是 common 面 + 少量扩展。 |
| [tilelang/engine/lower.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py) | 编译入口里调用 `determine_target` 的那一行。 |
| [docs/get\_started/targets.md](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/get_started/targets.md) | 官方 target 用法文档（含 `arch`/`code`、`TILELANG_DEFAULT_TARGET`）。 |

---

## 4. 核心概念与源码讲解

### 4.1 target 的统一入口：determine_target

#### 4.1.1 概念说明

用户在 `@tilelang.jit(target=...)`、`tilelang.compile(..., target=...)` 或 autotuner 里写的 `target` 形态五花八门：可能是一个字符串 `"cuda"`、一个配置字典 `{"kind": "cuda", "arch": "sm_90"}`、一个已构造好的 `tvm.target.Target`，也可能是缺省值 `"auto"`/`None`。

tilelang 不想让上层每个 API 都自己处理这四种形态，于是把它们统一收敛到一个函数 `determine_target`。它的职责是：

- 把任意合法的 target 输入，**归一化**成 TVM 能接受的形式（字符串 / 字典 / `Target`）；
- 对 `"auto"` 做硬件探测；
- 在需要时（`return_object=True`）再包成真正的 `tvm.target.Target` 对象。

核心心智模型：**「输入多样化 → 归一化 →（可选）物化为 Target 对象」**。归一化这一步还会顺带补全后端特有属性（如 HIP 的 `mtriple`/`thread_warp_size`），下一节细讲。

#### 4.1.2 核心流程

`determine_target(target, return_object=False)` 的判断树：

```
target == "auto" ?
├─ 是：当前线程是否已有一个 Target.current()？
│       ├─ 有 → 直接用它（尊重 TVM 的 with Target() 上下文）
│       └─ 无 → auto_detect_target()：依次跑注册的探测器 cuda→hip→metal
└─ 否：_validate_manual_target(target)
        ├─ 先跑注册的归一化器（normalizers），命中则用其结果
        ├─ 否则按 str / dict / Target 三类校验能否被 TVM 构造
        └─ 校验失败 → AssertionError
最后统一过 _finalize_target：
    ├─ 再跑一次归一化器（确保 dict/Target 也被补全属性）
    └─ return_object=True 且还不是 Target → Target(target)
```

关键点：**归一化器会被调用两次**——`_validate_manual_target` 里一次（用来识别像 `"cuda"` 这种需要补 `arch` 的输入），`_finalize_target` 里又一次（确保即便用户传的是 dict/`Target` 也能补全后端属性）。

#### 4.1.3 源码精读

入口函数 `determine_target` 本身非常薄，只是把 `"auto"` 与手动两类分流：

[文件路径:tilelang/backend/target.py:122-134](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/target.py#L122-L134) —— `target == "auto"` 时优先用 `Target.current()`，否则探测；手动输入走 `_validate_manual_target`，最后统一 `_finalize_target`。

`_validate_manual_target` 负责把 str/dict/Target 三类输入校验并归一化：

[文件路径:tilelang/backend/target.py:85-109](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/target.py#L85-L109) —— 先尝试注册的归一化器；对 dict/str 都会**真的去构造一次 `Target(...)`** 来验证合法性，构造失败就抛 `AssertionError`，提示「需要带属性时请传 dict」。注意字符串会先 `strip()`，空串直接拒绝。

`_finalize_target` 做最后一步归一化与（可选）物化：

[文件路径:tilelang/backend/target.py:112-119](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/target.py#L112-L119) —— 对 `str/dict/Target` 再跑一次归一化器（这是后端属性补全的关键），`return_object=True` 时包成 `Target`。

而在编译主链路里，调用点只有一行——`lower_to_host_device_ir` 在拿到字符串 target 时调用它：

[文件路径:tilelang/engine/lower.py:274-275](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py#L274-L275) —— `if isinstance(target, str): target = determine_target(target)`。注意：如果上层已经传入 `Target` 对象（非 str），这里**不会**再过 `determine_target`，直接进 `Target(target, host)` 构造。

> 补充：当用户完全没传 `target`（即 `None`）时，JIT 层会先读 `env.get_default_target()`（默认 `"auto"`），再把结果传给 `lower()`。所以 `None → "auto" → determine_target("auto") → 探测`，是一条完整链路。默认值由环境变量 `TILELANG_DEFAULT_TARGET` 控制（[env.py:396](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/env.py#L396)，[get_default_target](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/env.py#L493-L502)）。

#### 4.1.4 代码实践

**实践目标**：直观感受 `determine_target` 对不同输入的归一化结果。

**操作步骤**（在有 CUDA 的机器上；无 GPU 时 `"auto"` 会探测失败，可跳过该行）：

```python
# 示例代码
from tilelang.backend.target import determine_target

# 1) 字符串 → 归一化器会补出当前 GPU 的 arch
print(determine_target("cuda"))
# 期望形如：{"kind": "cuda", "arch": "sm_XX", ...} 或 Target(...) 字符串

# 2) 字典原样保留（合法即可）
print(determine_target({"kind": "llvm", "mcpu": "skylake"}))

# 3) 显式要 Target 对象
t = determine_target({"kind": "cuda", "arch": "sm_80"}, return_object=True)
print(type(t), t.kind.name, t.attrs.get("arch"))

# 4) 非法输入应被拒绝
try:
    determine_target({"kind": "cuda", "arch": "not_a_real_sm"})
except AssertionError as e:
    print("被拒绝：", e)
```

**需要观察的现象**：第 1 步即使你只写了 `"cuda"`，返回里也带上了 `arch`——这是归一化器（下一节）的功劳；第 3 步 `type(t)` 是 `tvm.target.Target`；第 4 步抛 `AssertionError`。

**预期结果**：合法输入被归一化或物化；非法字典在 TVM 构造阶段被拒。无 GPU 环境下第 1 步可能仍返回补了 arch 的 dict（取决于能否 import torch 并读到 capability），**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `determine_target` 在 `_validate_manual_target` 和 `_finalize_target` 里**都要**跑一次归一化器？去掉 `_finalize_target` 里那次会怎样？

**答案**：第一次（validate）主要用来「识别」像 `"cuda"` 这种需要补全属性的输入并校验合法性；但用户也可能直接传 dict 或 `Target` 对象（绕过字符串识别），所以 `_finalize_target` 里再跑一次，确保**所有路径**的输出都补上了后端特有属性（如 HIP 的 `mtriple`/`thread_warp_size`）。去掉后，直接传 `{"kind":"hip","mcpu":"gfx90a"}` 的用户就拿不到补全的 warp size，后续 pipeline 可能用错 warp 宽度。

**练习 2**：`determine_target("auto")` 里为什么会先看 `Target.current(allow_none=True)`？

**答案**：为了尊重 TVM 的 `with tvm.target.Target(...):` 上下文。如果调用方已经在一个显式 Target 上下文里，就优先用那个，而不是再去探测硬件——避免在 cross-compile / 测试场景里覆盖用户意图。

---

### 4.2 target='auto' 的探测与归一化注册表

#### 4.2.1 概念说明

`"auto"` 是 tilelang 最方便也最「魔法」的 target：它会在运行时探测本机有什么硬件，按 **CUDA → HIP → Metal** 顺序挑第一个可用的。这套机制靠两个注册表实现：

- **探测器（detector）**：`auto_detect_target()` 依次调用，返回非 `None` 即命中。只有 cuda/hip/metal 三个后端注册了探测器——`llvm`/`c`/`webgpu`/`cutedsl` 不参与自动探测（CPU 和 WebGPU 需要用户显式指定）。
- **归一化器（normalizer）**：把用户输入「翻译/补全」成更完整的 target。例如把裸 `"cuda"` 补成带 `arch` 的 dict，或把 `"cutedsl"` 翻译成「带 `cutedsl` key 的 cuda target」。

这套设计的好处是**解耦**：`backend/target.py` 完全不知道具体后端怎么探测，只提供注册接口；具体探测逻辑住在各自后端的 `target.py` 里，import 时自动注册。

#### 4.2.2 核心流程

**注册时机**：`import tilelang`（非 light import）时，[tilelang/\_\_init\_\_.py:217-220](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/__init__.py#L217-L220) 按 `cpu → cuda → rocm → metal` 顺序 import 各后端包，触发各自的 `__init__.py` → `target.py` 末尾的 `register_target_detector(...)`。由于 `cpu` 不注册探测器，最终 `_TARGET_DETECTORS` 的插入顺序是 **cuda → hip → metal**，这正是探测顺序。

**探测流程**：

```
auto_detect_target():
    for spec in _TARGET_DETECTORS.values():   # cuda, hip, metal
        try: detected = spec.detect()
        except: 记录错误，继续
        if detected is not None: return detected
    全都没命中 → ValueError("No registered target detector found an available target.")
```

**各探测器判定条件**：

| 后端 | 探测器返回非 None 的条件 |
| --- | --- |
| cuda | `torch.version.hip is None` 且能找到 CUDA 路径（`nvcc.find_cuda_path`），再从 PyTorch 读 SM arch |
| hip | 能找到 ROCm 路径（`tvm.contrib.rocm.find_rocm_path`），再从 PyTorch 读 `gcnArchName` |
| metal | `platform.mac_ver()` 显示是 Mac 且架构为 `arm64`（Apple Silicon） |

注意 CUDA 探测里有一句 `if torch.version.hip is not None: return None`——**PyTorch 若是 HIP 版本（装在 AMD 上），CUDA 探测主动让位**，交给后面的 hip 探测器接管。

#### 4.2.3 源码精读

注册表的载体与遍历函数：

[文件路径:tilelang/backend/target.py:66-78](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/target.py#L66-L78) —— `auto_detect_target`：逐个跑探测器，捕获异常只记录不中断，全部 miss 才报错并把「试过哪些、各报什么错」拼进报错信息。`list_target_detectors()` 暴露当前已注册的探测器名字。

[文件路径:tilelang/backend/target.py:32-42](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/target.py#L32-L42) —— `register_target_detector`：写入 `_TARGET_DETECTORS` 字典，重名默认拒绝（`override=True` 才覆盖）。

CUDA 探测器（含让位 HIP 的关键判断与从 PyTorch 读 arch）：

[文件路径:tilelang/cuda/target.py:48-64](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/target.py#L48-L64) —— `_detect_cuda_target`：HIP 版 PyTorch 直接返回 `None`；否则查 CUDA 可用性，再 `_detect_torch_cuda_arch()` 读 `torch.cuda.get_device_capability()` 拼成 `sm_XX`。`normalize_cuda_target` 只对**裸字符串 `"cuda"`** 生效，把它补成带 arch 的 Target。

HIP 探测器与属性补全（归一化器的典型用法）：

[文件路径:tilelang/rocm/target.py:98-122](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/rocm/target.py#L98-L122) —— `_detect_rocm_target` 探测 ROCm；`normalize_rocm_target` 对任何 `kind=="hip"` 的输入，调用 `with_rocm_target_attrs` 补上 `mtriple`（固定 `amdgcn-amd-amdhsa-hcc`，见 [rocm/target.py:7](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/rocm/target.py#L7)）和 `thread_warp_size`（gfx9 系=64，gfx10/11/12=32，见 [rocm/target.py:31-38](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/rocm/target.py#L31-L38)）。这就是「归一化器补全后端属性」的实例。

Metal 探测器（最简单，只判断 arm64 Mac）：

[文件路径:tilelang/metal/target.py:16-27](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/metal/target.py#L16-L27) —— `check_metal_availability` 看 `mac_ver()` 与架构；命中则返回字符串 `"metal"`（不带任何属性）。

三处注册调用（在各自 `target.py` 文件末尾，import 时执行）：

[文件路径:tilelang/cuda/target.py:159-161](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/target.py#L159-L161) —— 注册 `cuda` 探测器 + `cuda` 归一化器 + `cutedsl` 归一化器。
[文件路径:tilelang/rocm/target.py:149-150](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/rocm/target.py#L149-L150) —— 注册 `hip` 探测器 + `hip` 归一化器。
[文件路径:tilelang/metal/target.py:34](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/metal/target.py#L34) —— 注册 `metal` 探测器（Metal 无独立归一化器）。

#### 4.2.4 代码实践

**实践目标**：看清当前环境注册了哪些探测器/归一化器，以及 `"auto"` 实际选中了谁。

**操作步骤**：

```python
# 示例代码
from tilelang.backend.target import list_target_detectors, determine_target

print("已注册探测器顺序:", list_target_detectors())
# 期望：('cuda', 'hip', 'metal') —— 决定了 auto 的探测顺序

print("auto 选中:", determine_target("auto"))
```

**需要观察的现象**：探测器顺序与你机器上的硬件是否吻合——NVIDIA 机器命中 cuda（带 arch），AMD 机器命中 hip（带 mcpu/mtriple/warp_size），arm64 Mac 命中 metal，三者皆无则抛 `ValueError` 并列出每个探测器尝试过、各自的错误。

**预期结果**：在有 GPU 的机器上返回带属性的目标；无任何可用目标时报错信息会含 `Tried: cuda: ..., hip: ..., metal: ...`。**待本地验证**具体选中谁。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `llvm`、`c`、`webgpu` 不会被 `auto` 选中？

**答案**：因为它们没有调用 `register_target_detector` 注册探测器。`auto` 只遍历 `_TARGET_DETECTORS`，里面只有 cuda/hip/metal。CPU 和 WebGPU 通常需要用户显式 `target="llvm"` / `target="c"` / `target="webgpu"`，避免在带 GPU 的机器上误选 CPU。

**练习 2**：把一台 AMD 机器上的 PyTorch 装成 HIP 版，`determine_target("cuda")` 会发生什么？

**答案**：`normalize_cuda_target` 内部调用 `_detect_torch_cuda_arch`，而 `_detect_cuda_target`/探测路径里有 `if torch.version.hip is not None: return None`。对裸 `"cuda"` 字符串，归一化器若拿不到 arch 会返回 `None`，于是 `"cuda"` 不被补全、按原样交给 TVM 构造，可能生成一个不带 arch 的 cuda target——在 AMD 机器上后续编译会失败。这正是 tilelang 让 CUDA 探测在 HIP 版 PyTorch 下「主动让位」的原因。

---

### 4.3 多后端 language 子包：common + 方言扩展

#### 4.3.1 概念说明

写 tilelang kernel 时，我们总是 `import tilelang.language as T`。但 `tilelang.language` 其实是一个**门面（facade）**——它默认就是 CUDA 方言。其它后端有各自的语言子包：`tilelang.cuda.language`、`tilelang.rocm.language`、`tilelang.metal.language`、`tilelang.cpu.language`、`tilelang.webgpu.language`。

它们共享一个公共面 `tilelang.language.common`（`T.Kernel`、`T.copy`、`T.gemm`、`T.alloc_shared`、循环原语等所有后端通用的 DSL 都在这里），各后端只在它之上**追加自己的硬件扩展**：

| 方言 | 公共面 common | 额外扩展 |
| --- | --- | --- |
| cuda（=默认 `tilelang.language`） | ✓ | cluster、warpgroup、pdl、TMA、intrinsics（ldg/stg/lds/sts…）、random、print、tir |
| rocm | ✓ | ROCm MFMA intrinsics |
| metal | ✓ | metal tir |
| cpu | ✓ | （目前无额外扩展，纯 common） |
| webgpu | ✓ | （目前无额外扩展，纯 common） |

每个方言模块都会设置 `__tilelang_dialect__`（如 `"cuda"`/`"rocm"`/`"metal"`），供编译器内部判断当前是哪种方言。

> 注意区分两个维度：**编译 target**（决定代码生成走向）与 **import 的方言**（决定你写 DSL 时能用哪些 intrinsic）。二者通常匹配——写 CUDA TMA 就该用 `tilelang.language`（CUDA 方言）并编译到 `cuda`；但 common 面的 kernel（如 elementwise）在任何方言下写法都一样。

#### 4.3.2 核心流程

门面层的转发只有一行实质内容：

```
tilelang/language/__init__.py:
    from tilelang.cuda.language import *        # 直接把 CUDA 方言作为默认
    __tilelang_dialect__ = "cuda"
```

每个后端方言的组装模式一致：

```
tilelang/<backend>/language/__init__.py:
    from tilelang.language.common import *       # 1) 先引入公共面
    from .<extensions> import *                 # 2) 再追加本后端扩展
    __tilelang_dialect__ = "<backend>"
    __all__ = 去重合并(common + 扩展)
```

以 CUDA 方言为例，它在 common 之上叠了 8 个扩展模块（cluster/intrinsics/pdl/print/random/tir/warpgroup，以及直接从 `tilelang.language.*` 引入的 TMA、ldg/stg 等 builtin），所以默认 `T` 命名空间最「胖」。

#### 4.3.3 源码精读

默认门面 = CUDA 方言的再导出：

[文件路径:tilelang/language/\_\_init\_\_.py:9-14](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/__init__.py#L9-L14) —— 文档字符串明确说明：「re-exports the CUDA dialect」，并设 `__tilelang_dialect__ = "cuda"`。这就是为什么 `import tilelang.language as T` 默认就能用 TMA/WGMMA 等 CUDA 特性。

CUDA 方言的组装（common + 8 类扩展）：

[文件路径:tilelang/cuda/language/\_\_init\_\_.py:5-62](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/language/__init__.py#L5-L62) —— 先 `from tilelang.language.common import *`；再引入 CUDA 专属 builtin（`tma_load`/`ldg128`/`sts64`/`fence_proxy_async` 等）、copy 扩展（`tma_copy`/`copy_cluster`）、kernel 类（`ClusterKernel`）；最后 `from .cluster/.intrinsics/.pdl/.print/.random/.tir/.warpgroup import *`。

ROCm 方言（common + ROCm intrinsics）：

[文件路径:tilelang/rocm/language/\_\_init\_\_.py:5-11](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/rocm/language/__init__.py#L5-L11) —— 只在 common 之上加 `.intrinsics`（MFMA 相关），`__tilelang_dialect__ = "rocm"`。

CPU / WebGPU 方言（纯 common，最瘦）：

[文件路径:tilelang/cpu/language/\_\_init\_\_.py:5-9](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cpu/language/__init__.py#L5-L9) —— CPU 方言只导出 common，`__all__ = _COMMON_ALL`，dialect 为 `"cpu"`。WebGPU 方言结构相同（[webgpu/language/\_\_init\_\_.py:5-9](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/webgpu/language/__init__.py#L5-L9)）。

Metal 方言（common + metal tir）：

[文件路径:tilelang/metal/language/\_\_init\_\_.py:5-12](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/metal/language/__init__.py#L5-L12) —— common 之上加 `.tir`，dialect 为 `"metal"`。

#### 4.3.4 代码实践

**实践目标**：对比各方言的「胖瘦」与 dialect 标识。

**操作步骤**：

```python
# 示例代码
import tilelang.language as cuda_T
import tilelang.rocm.language as rocm_T
import tilelang.cpu.language as cpu_T

print("默认 dialect:", cuda_T.__tilelang_dialect__)
print("rocm  dialect:", rocm_T.__tilelang_dialect__)
print("cpu   dialect:", cpu_T.__tilelang_dialect__)

# CUDA 独有、common 没有的扩展
cuda_only = set(dir(cuda_T)) - set(dir(cpu_T))
print("CUDA 比 CPU 多出的名字（部分）:",
      sorted(n for n in cuda_only if n.lower().startswith(("tma", "ldg", "wgmma", "cluster")))[:10])
```

**需要观察的现象**：三个 dialect 分别是 `cuda/rocm/cpu`；CUDA 方言比 CPU 多出 `tma_load`、`ldg128`、`ClusterKernel` 等 CUDA 专属名字，而 CPU/WebGPU 方言几乎没有额外扩展。

**预期结果**：`__tilelang_dialect__` 正确反映各方言；CUDA 多出的名字集中在 TMA/ldg/stg/cluster/warpgroup。**待本地验证**具体名字集合（随版本变化）。

#### 4.3.5 小练习与答案

**练习 1**：如果我想写一个「既能在 NVIDIA 又能在 AMD 上跑」的 elementwise kernel，需要为两个后端写两份代码吗？

**答案**：不需要。elementwise 只用 common 面（`T.Kernel`/`T.copy`/`T.Parallel`/`T.alloc_shared`），common 在所有方言里都一样。你可以用默认 `import tilelang.language as T` 写一份，然后用 `target="cuda"` 或 `target="hip"` 分别编译。只有用到 CUDA 专属 intrinsic（如 TMA）或 ROCm 专属 intrinsic（如 MFMA）时才需要按方言区分。

**练习 2**：`tilelang.language` 和 `tilelang.cuda.language` 是同一个东西吗？

**答案**：导出的命名空间内容相同（前者 `from tilelang.cuda.language import *`），但严格说 `tilelang.language` 是门面模块、`tilelang.cuda.language` 是 CUDA 方言模块本身；两者都设 `__tilelang_dialect__ = "cuda"`。日常用 `import tilelang.language as T` 即可。

---

### 4.4 CuTeDSL：叠加在 CUDA 之上的后端变体

#### 4.4.1 概念说明

CuTeDSL（NVIDIA CUTLASS/CuTe DSL）是 tilelang 支持的一种**特殊后端**，但它**不是一个独立的 target kind**。它的实现策略很巧妙：

- target 的 `kind` 仍然是 `"cuda"`；
- 只是在 target 的 `keys` 列表里**额外塞一个 `"cutedsl"` 标签**；
- 代码生成阶段靠这个标签，把同样 `kind=="cuda"` 的 target 分流到「普通 CUDA codegen」或「CuTeDSL codegen」。

换句话说，CuTeDSL 是「CUDA 的一个变体（variant）」，而不是并列的第七种 target。这样设计的好处是：CuTeDSL 能复用 CUDA 的全部 target 属性（arch、host 等），只在「用哪套 codegen」这一处分叉。

适用场景：当你想用 CUTLASS CuTe DSL 的 kernel 组合能力、或需要 `nvidia-cutlass-dsl` 包提供的特性时，显式指定 `target="cutedsl"`（或 `execution_backend="cutedsl"`）。

#### 4.4.2 核心流程

```
用户写 target="cutedsl"
   │
   ▼ normalize_cutedsl_target（cuda/target.py 归一化器）
   │   ① 探测当前 CUDA arch
   │   ② 构造 kind="cuda" 的 target
   │   ③ 在 target.keys 里追加 "cutedsl"  → _with_cutedsl_key
   ▼ _normalize_cutedsl_target_for_resolve
   │   额外调用 check_cutedsl_available()，没装 tilelang-cutedsl 包 → AssertionError
   ▼ 得到一个 kind=cuda、keys 含 cutedsl 的 Target
   ▼ resolve_device_codegen(target)
   │   在 "cuda" 这个 kind 下有两条注册：
   │     - name="cuda",    supports_target=_is_plain_cuda_target（keys 不含 cutedsl）
   │     - name="cutedsl", supports_target=_is_cutedsl_target（keys 含 cutedsl）
   │   matches() 用谓词筛选 → 选中 cutedsl codegen
   ▼ 走 target.build.tilelang_cutedsl 而非 target.build.tilelang_cuda
```

关键点：**分流不在 `determine_target`，而在 `resolve_device_codegen`**——因为多个 codegen 可以注册在同一个 target kind 下，靠 `supports_target` 谓词区分。

#### 4.4.3 源码精读

CuTeDSL 归一化器：把 `"cutedsl"` / `{"kind":"cutedsl"}` / 已带标签的 Target 统一成「kind=cuda + cutedsl key」：

[文件路径:tilelang/cuda/target.py:67-103](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/target.py#L67-L103) —— `_with_cutedsl_key` 用 `target.export()` 取出属性字典，在 `keys` 里去重追加 `"cutedsl"` 再 `Target(...)` 重建；`normalize_cutedsl_target` 处理三种输入形态：字符串 `"cutedsl"`、dict `{"kind":"cutedsl",...}`（把 kind 改回 cuda 再加标签）、已是带标签的 `Target`（原样返回）。

可用性检查包装：

[文件路径:tilelang/cuda/target.py:106-116](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/target.py#L106-L116) —— `_normalize_cutedsl_target_for_resolve` 在归一化后调用 `check_cutedsl_available()`，缺包则抛 `AssertionError` 提示安装 `tilelang-cutedsl`。这才是真正注册到归一化器表里的函数（[cuda/target.py:161](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/target.py#L161)）。

device codegen 的谓词分流（本讲最核心的「同 kind 多变体」模式）：

[文件路径:tilelang/cuda/codegen.py:8-36](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/codegen.py#L8-L36) —— 两个谓词 `_is_plain_cuda_target` / `_is_cutedsl_target` 互斥地看 `"cutedsl" in target.keys`；然后在同一个 target kind `"cuda"` 下注册两个 `DeviceCodegen`（`name="cuda"` 和 `name="cutedsl"`），各自绑定不同的 TVM 全局函数（`target.build.tilelang_cuda` vs `target.build.tilelang_cutedsl`）。

注册表如何用谓词挑选：

[文件路径:tilelang/backend/device_codegen.py:85-110](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/device_codegen.py#L85-L110) —— `_matching_device_codegens` 先按 `target.kind.name` 取该 kind 下所有 codegen，再用 `codegen.matches(target)`（即 `supports_target` 谓词）过滤；`resolve_device_codegen` 取第一个匹配项，没人匹配就报错并列出该 kind 下所有已注册名字。

与 4.1/4.2 呼应：device codegen 与 execution backend 都采用「按 target kind 惰性 import + 谓词匹配」的同一套注册表模式（见 [backend/\_\_init\_\_.py:42-47](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/__init__.py#L42-L47) 的 `register_lazy_device_codegen`）。execution backend 还把 `"dlpack"` 规范化成 `"tvm_ffi"`（[execution_backend.py:12-25](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/execution_backend.py#L12-L25)），并在 `requested in (None,"auto")` 时取第一个可用后端（[execution_backend.py:101-104](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/execution_backend.py#L101-L104)）。

#### 4.4.4 代码实践

**实践目标**：构造一个 CuTeDSL target，看清它的 `kind` 与 `keys`，并理解为何它不会跑到普通 CUDA codegen。

**操作步骤**：

```python
# 示例代码（无需真的安装 cutedsl 也能观察归一化这一步）
from tilelang.cuda.target import normalize_cutedsl_target, _with_cutedsl_key
from tvm.target import Target

# 1) 直接看 _with_cutedsl_key 给一个普通 cuda target 加了什么
base = Target({"kind": "cuda", "arch": "sm_90"})
cdsl = _with_cutedsl_key(base)
print("kind:", cdsl.kind.name)          # 仍是 cuda
print("keys:", cdsl.keys)               # 多出 "cutedsl"

# 2) normalize_cutedsl_target 对字符串的处理（arch 取决于本机，可能为 None→"cuda"）
print(normalize_cutedsl_target("cutedsl"))
```

**需要观察的现象**：`cdsl.kind.name` 仍是 `"cuda"`，但 `cdsl.keys` 里出现了 `"cutedsl"`。这正是 `_is_cutedsl_target` 谓词返回 True、从而选中 cutedsl codegen 的依据。

**预期结果**：`kind` 不变、`keys` 含 `cutedsl`。若本机无 CUDA，第 2 步可能回退为不带 arch 的 cuda target，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 CuTeDSL 不直接做成 `kind=="cutedsl"` 的独立 target，而要挂在 `kind=="cuda"` 之下？

**答案**：因为 CuTeDSL 复用了 CUDA 的全部目标属性（SM arch、host、warpgroup 约束等），只有「代码生成那一刀」不同。做成同 kind 的变体，可以用 `supports_target` 谓词在 device codegen 注册表里干净地分流，避免在 `determine_target`、pipeline 选择、host codegen 等所有地方都加一个 `if cutedsl` 分支——把变化点收敛到 codegen 一处。

**练习 2**：`execution_backend="dlpack"` 和 `execution_backend="tvm_ffi"` 是什么关系？

**答案**：等价。`canonicalize_execution_backend` 把 `"dlpack"` 规范化成 `"tvm_ffi"`（历史命名兼容）。真正的执行后端注册表里只有 `tvm_ffi` 这一项，`dlpack` 只是对用户友好的别名。

---

## 5. 综合实践：同一个 kernel，两套 target，对比生成的设备源码

把本讲四个模块串起来：用 **elementwise 加法**（只依赖 common 面，任何后端都能编）作为实验对象，分别指定 `target="cuda"` 与一个 CPU 后端，编译并对比两份**生成的设备源码**，体会「target 决定 codegen」的真正含义。

> 说明：spec 要求对比 `cuda` 与 `cpu(llvm)`。实际上 `target="c"`（纯 C 源码）最适合「看源码差异」，因为它直接产出可读的 C；`target="llvm"` 产出的是 LLVM IR，可读性差但能真正在 CPU 上跑。本实践**两者都做**：先看 `c`（满足「查看源码差异」的目标），再说明 `llvm` 的差异。

### 实践目标

- 验证同一份 DSL 在不同 target 下走不同的 device codegen，产出形态完全不同的设备源码。
- 学会用 `get_kernel_source()` 取回生成代码并落盘对比。

### 操作步骤

1. 准备 kernel（直接复用 [examples/elementwise/example_elementwise_add.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/elementwise/example_elementwise_add.py) 的 `elementwise_add`，改写成 lazy 风格便于显式编译）。

   ```python
   # 示例代码
   import tilelang
   import tilelang.language as T

   @T.prim_func
   def elementwise_add(A: T.Tensor((256, 256), "float32"),
                       B: T.Tensor((256, 256), "float32"),
                       C: T.Tensor((256, 256), "float32")):
       with T.Kernel(8, 8, threads=128) as (bx, by):
           A_sh = T.alloc_shared((32, 32), "float32")
           B_sh = T.alloc_shared((32, 32), "float32")
           C_lc = T.alloc_fragment((32, 32), "float32")
           T.copy(A[by * 32, bx * 32], A_sh)
           T.copy(B[by * 32, bx * 32], B_sh)
           for i, j in T.Parallel(32, 32):
               C_lc[i, j] = A_sh[i, j] + B_sh[i, j]
           T.copy(C_lc, C[by * 32, bx * 32])
   ```

2. 分别用两个 target 编译，取源码：

   ```python
   # 示例代码
   cuda_kernel = tilelang.compile(elementwise_add, target="cuda")
   c_kernel    = tilelang.compile(elementwise_add, target="c")

   cuda_src = cuda_kernel.get_kernel_source()
   c_src    = c_kernel.get_kernel_source()

   open("kernel_cuda.cu", "w").write(cuda_src)
   open("kernel_c.c",     "w").write(c_src)
   ```

3. （可选）再编一个 `llvm` 版本观察差异：

   ```python
   # 示例代码
   llvm_kernel = tilelang.compile(elementwise_add, target={"kind": "llvm"})
   open("kernel_llvm.txt", "w").write(llvm_kernel.get_kernel_source())
   ```

### 需要观察的现象

- **cuda 源码**：含 `__global__` kernel、`__shared__` 内存、`blockIdx`/`threadIdx`、线程循环搬运，是典型 CUDA C++。
- **c 源码**：是普通 C 函数（无 `__global__`），用普通的指针运算与循环表达 tile 计算；因为 `target.kind.name == "c"` 时 `is_cpu_device_backend` 返回 True（[lower.py:24-25](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py#L24-L25)），host/device 不按 GPU kernel launch 拆分。
- **llvm 源码**：是 LLVM IR（`define ...`、`getelementptr`、`load`/`store`），可读性远低于 C，但能在 CPU 上原生执行。
- 三者都来自同一个 `elementwise_add` PrimFunc，差别完全由 `determine_target` → `resolve_device_codegen` 这条链路决定。

### 预期结果

| target | device codegen 名 | 源码形态 | 能否直接运行 |
| --- | --- | --- | --- |
| `"cuda"` | `cuda`（[cuda/codegen.py:16-25](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/codegen.py#L16-L25)） | CUDA C++（`__global__`/`__shared__`） | 需 NVIDIA GPU |
| `"c"` | `c`（[cpu/codegen.py:12-19](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cpu/codegen.py#L12-L19)） | 纯 C 源码 | 仅源码/自定义工具链 |
| `{"kind":"llvm"}` | `llvm`（[cpu/codegen.py:21-29](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cpu/codegen.py#L21-L29)） | LLVM IR | 需 LLVM 启用 |

如果 `llvm` 后端未启用（构建时未开 `USE_LLVM`），第 3 步会在 `resolve_device_codegen` 报 `No device codegen registered for target 'llvm'`——这正好验证了「device codegen 是按 target kind 惰性注册」的机制。**待本地验证**（取决于你的构建选项与是否有 GPU）。

### 进阶思考

把 `target="c"` 改成 `target="cutedsl"`（需安装 `tilelang-cutedsl`），观察 `_normalize_cutedsl_target_for_resolve` 如何先检查依赖、再产出带 `cutedsl` key 的 target，最终走 `target.build.tilelang_cutedsl`。

---

## 6. 本讲小结

- **统一入口**：`determine_target` 把 `"auto"`/str/dict/`Target` 四种输入归一化，必要时物化为 `Target`；归一化器会被调用两次以确保后端属性补全。
- **auto 探测**：按 **CUDA → HIP → Metal** 顺序（注册表 dict 的插入顺序），逐个跑探测器，首个非 None 命中；`llvm`/`c`/`webgpu` 不参与自动探测。
- **注册表解耦**：`backend/target.py` 只提供注册接口，cuda/rocm/metal 各自在 `target.py` 末尾注册探测器与归一化器，HIP 归一化器是「补全后端属性」的典型例子。
- **方言门面**：`tilelang.language` 默认就是 CUDA 方言；所有后端共享 `language.common`，CUDA 方言最「胖」（含 TMA/cluster/warpgroup…），CPU/WebGPU 最「瘦」（几乎纯 common）。
- **CuTeDSL 是变体不是新 kind**：它的 `kind` 仍是 `cuda`，只在 `keys` 里加 `"cutedsl"`；device codegen 注册表用 `supports_target` 谓词在同 kind 下分流到不同 codegen。
- **同一套注册表模式**：detector/normalizer、device codegen、execution backend 都采用「按 target kind 注册 +（可选）惰性 import + 谓词匹配」，`resolve_*` 函数是各自的查表入口。

## 7. 下一步学习建议

- **深入 device codegen 内部**：进入 u6-3（设备代码生成与模板），看 `codegen_cuda` 如何把 TIR 生成 CUDA C++，以及 `tl_templates` 模板如何注入。
- **理解 host/device 拆分**：本讲提到 `is_cpu_device_backend` 影响 host/device 拆分，详细机制在 u7-2（host/device 拆分、库生成与编译回调）。
- **执行后端细节**：`resolve_execution_backend_spec` 选出的 `tvm_ffi`/`nvrtc`/`torch`/`cutedsl` 等 adapter 如何把编译产物包成可调用对象，见 u7-1（执行后端与 kernel adapter）。
- **扩展新后端**：若想为 tilelang 增加一个新后端，需要同时补齐：① `target.py` 里的探测器/归一化器；② `language/__init__.py` 方言；③ `codegen.py` 的 device codegen 注册；④ `execution_backend.py` 的执行后端——这套「四件套」正是 u10-2（扩展 TileLang）的内容。
