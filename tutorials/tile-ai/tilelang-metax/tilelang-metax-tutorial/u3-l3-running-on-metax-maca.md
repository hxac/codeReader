# 在 Metax GPU 上运行（MACA target）

## 1. 本讲目标

本讲承接 [u3-l1（target 体系）](u3-l1-targets-and-config.md) 与 [u3-l2（JIT 编译与 kernel 对象）](u3-l2-jit-and-kernel-object.md)，把目光聚焦到 **tilelang-metax 这个分支存在的核心理由——MACA 后端**：如何让一份用 TileLang 写的 kernel，最终编译并运行在 MetaX 的 GPU 上。

读完本讲，你应当能够：

- 用 `target={"kind":"maca"}` 把一个 kernel 编给 MetaX GPU；
- 说清楚 TileLang 判断「MACA 是否可用」的完整条件链；
- 列出 MACA 支持的执行后端（execution backend），并理解 `tvm_ffi` 为何是默认；
- 在「有设备」和「无设备」两种环境下分别验证 MACA 编译，并排查常见的 maca 运行问题。

> 本文所有结论均基于当前 HEAD 的真实源码。涉及需要在真实 MetaX 硬件上才能看到的运行结果，会明确标注「待本地验证」。

## 2. 前置知识

在进入 MACA 之前，请先建立这几个概念（前几讲已讲过，这里只做最简回顾）：

- **target**：回答「kernel 编给谁」。由 `kind`（平台类型，如 `cuda`/`hip`/`maca`）与若干 `attrs`（设备属性，如 `mcpu`/`arch`）组成。TileLang 直接复用 TVM 的 `tvm.target.Target`。详见 u3-l1。
- **执行后端（execution backend）**：回答「生成的代码用什么方式变成可运行件、并接到 Python」。它与 target 正交，但受各 target 注册清单约束。详见 u3-l2。
- **MACA**：MetaX GPU 的类 CUDA 软件栈。可以粗暴地类比：**MACA 之于 MetaX，就像 CUDA 之于 NVIDIA、HIP 之于 AMD**。它的编译器叫 `mxcc`，产物是 `mcbin`/`mcir`。
- **warp_size**：一个 warp（线程束）里的线程数。**MACA 的 warp_size 是 64，CUDA 是 32**——这是两个后端最常踩的差异点。
- **MACA SDK 的安装位置**：通常是 `/opt/maca`，编译器在 `mxgpu_llvm/bin/mxcc`。环境变量 `MACA_PATH`（或 `MACA_HOME`）用来告诉 TileLang SDK 在哪。详见 [u1-l2（环境搭建与编译安装）](u1-l2-build-and-install.md)。

一句话定位：**MACA 是 tilelang-metax 把 `maca` 注册成与 `cuda`/`hip` 平级的一等 target kind 之后，那一条完整的「检测 → 选后端 → 生成代码 → 编译 → 运行」链路。** 本讲就是要把这条链路拆开看。

## 3. 本讲源码地图

本讲涉及的文件集中在 Python 侧的 `tilelang/maca/` 与少量 C++/contrib 文件：

| 文件 | 作用 |
| --- | --- |
| `tilelang/maca/__init__.py` | MACA 子包入口，import 时把 target/pipeline/codegen/execution_backend 等挂入全局注册表 |
| `tilelang/maca/target.py` | MACA 的 **target 检测器**：判断 MACA 是否可用、是否应当被 auto 选中 |
| `tilelang/maca/execution_backend.py` | 为 `maca` kind 注册可选的 **执行后端**（tvm_ffi/mcrtc/cython/cutedsl） |
| `tilelang/maca/codegen.py` | 为 `maca` kind 注册 **设备代码生成**入口（生成源码 / 编译产物） |
| `tilelang/contrib/mxcc.py` | 调用系统 `mxcc` 编译器的工具：找 SDK 路径、编译、查架构 |
| `tilelang/env.py` | 运行期环境变量集中管理，包含 `MACA_HOME` 的三级探测 |
| `src/maca/runtime/maca_target_kind.cc` | C++ 侧用 `TVM_REGISTER_TARGET_KIND` 注册 `maca` kind 及其默认属性（warp_size=64 等） |
| `src/maca/codegen/rt_mod_maca.cc` | C++ 侧 MACA 代码生成两个全局函数：编译版与「只生成源码不编译」版 |
| `tilelang/engine/lower.py` | 编译主流程，区分「编译设备」与「只取源码」两条路 |
| `docs/get_started/Installation_maca.md` | MACA 专属安装与运行验证文档 |

## 4. 核心概念与源码讲解

### 4.1 maca target：把 kernel 编给 MetaX GPU

#### 4.1.1 概念说明

`maca` 是一个 target kind，和 `cuda`、`hip` 平级。当你把 target 指定为 `maca` 时，等于告诉 TileLang：「请按 MetaX GPU 的语法（MACA）来生成设备代码、用 MetaX 的工具链（mxcc）来编译、最终在 MetaX GPU 上跑。」

target kind 本身只是个「名字 + 默认属性表」。真正让它工作的是三处注册：

1. **C++ 侧注册 kind 与默认属性**（`thread_warp_size=64` 等写在 `maca_target_kind.cc`）；
2. **Python 侧注册「检测器」**（告诉 auto 探测什么时候该选 maca）；
3. **Python 侧注册「代码生成」与「执行后端」**（告诉编译器 maca 用哪条 codegen、哪些执行后端）。

第 2、3 点都从 `tilelang/maca/__init__.py` 这个入口触发：

```python
[tilelang/maca/__init__.py:1-6]
from . import intrinsics  # noqa: F401
from . import op  # noqa: F401
from . import pipeline  # noqa: F401
from . import target  # noqa: F401            # 注册 target 检测器
from . import execution_backend  # noqa: F401 # 注册执行后端
from . import transform  # noqa: F401
```

而这个子包之所以会被加载，是因为顶层包在末尾显式 import 了它（这就是 u1-l3 提到的「挂载 MACA 的那一行」）：

> [tilelang/__init__.py:215](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/__init__.py#L215) —— `from . import maca as maca`，触发上述所有注册。

#### 4.1.2 核心流程

当你写下 `target={"kind": "maca"}` 时，发生的事情：

1. `Target({"kind": "maca"})` 构造时，触发 C++ 侧注册的 **canonicalizer**（规范化器）`UpdateMACAAttrs`：补齐默认的 `mtriple` 与 `mcpu`。
2. TileLang 的 `lower` 流程拿到这个 target，按 maca 的 pipeline 做 lowering（详见 u7 系列）。
3. 设备代码生成阶段，根据是否要真正编译，分派到 `tilelang_maca`（编译）或 `tilelang_maca_without_compile`（只生成源码）。
4. 执行后端（默认 `tvm_ffi`）把生成的 mcbin 包装成可被 Python 调用的 kernel 对象。

用一个伪流程表示：

```
target={"kind":"maca"}
   │
   ▼  Target(...) 触发 canonicalizer
补齐 mtriple="mxc-metax-macahca", mcpu="xcore1000"（或查 macainfo）
   │
   ▼  lower() 走 maca pipeline
设备 IR
   │
   ├── compile_device=True  ─► target.build.tilelang_maca        ─► mxcc 编译 ─► mcbin ─► tvm_ffi 包装 ─► 可运行 kernel
   └── compile_device=False ─► target.build.tilelang_maca_without_compile ─► 仅源码（不调 mxcc）
```

#### 4.1.3 源码精读

**C++ 侧：注册 maca kind 与默认属性。**

> [src/maca/runtime/maca_target_kind.cc:59-71](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/runtime/maca_target_kind.cc#L59-L71) —— 把 `maca` 注册为绑定到设备类型 `kDLMACA` 的 target kind，并声明一组默认属性：`mcpu`、`mtriple`、`mattr`、`max_num_threads=1024`、`max_threads_per_block=1024`、`max_shared_memory_per_block=65536`、**`thread_warp_size=64`**、`max_local_memory_per_block=4095`，默认 keys 为 `{"maca", "gpu"}`。

注意第 67 行的 `thread_warp_size` 默认值是 **64**——这就是 MACA 与 CUDA（32）在编译期最关键的差异来源，后面很多 warp 相关的代码生成都依赖它。

**C++ 侧：canonicalizer 补默认属性。**

> [src/maca/runtime/maca_target_kind.cc:38-57](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/runtime/maca_target_kind.cc#L38-L57) —— `UpdateMACAAttrs`：若未指定 `mtriple` 则填 `mxc-metax-macahca`；若未指定 `mcpu`，则调用 Python 回调 `tvm_callback_maca_get_arch`（读 `macainfo` 探测真实架构，探测不到则回退 `xcore1000`）。

这意味着：即使你只写 `{"kind":"maca"}`，target 也会被自动补成一个「带 mtriple 和 mcpu 的完整 target」。

**Python 侧：maca 设备代码生成入口。**

> [tilelang/maca/codegen.py:12-21](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/codegen.py#L12-L21) —— 为 `maca` kind 注册一个 `DeviceCodegen`：`build` 指向 C++ 全局函数 `target.build.tilelang_maca`（会编译），`build_without_compile` 指向 `target.build.tilelang_maca_without_compile`（只出源码）。

这两个 build 函数的 C++ 实现见：

> [src/maca/codegen/rt_mod_maca.cc:142-169](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/rt_mod_maca.cc#L142-L169) —— `BuildTileLangMACAWithoutCompile`：用 `CodeGenTileLangMACA` 生成源码、跑 postproc，但**不调用** `tilelang_callback_maca_compile`（即不调 mxcc），只返回一个装着源码的占位 module。这就是「无设备也能拿到源码」的根源。

对照编译版（`BuildTileLangMACA`，同文件上半部分）会额外调用 `tilelang_callback_maca_compile` 把源码交给 mxcc 编成 mcbin。

#### 4.1.4 代码实践

**目标**：体验 target 规范化——亲手构造一个 maca target，观察它被自动补齐了哪些属性。

**操作步骤**（这是「源码阅读 + 构造验证」型实践，不依赖真实设备）：

1. 确保 tilelang 是用 `USE_MACA=ON` 编译的（否则 `Target("maca")` 会因未知 kind 而报错）。
2. 在 Python 里：

```python
# 示例代码：观察 maca target 的规范化结果
from tvm.target import Target

t = Target({"kind": "maca"})
print("kind   :", t.kind.name)
print("mtriple:", t.attrs.get("mtriple"))
print("mcpu   :", t.attrs.get("mcpu"))
print("warp   :", t.attrs.get("thread_warp_size"))
```

**需要观察的现象**：`mtriple` 被自动填成 `mxc-metax-macahca`；`mcpu` 至少是回退值 `xcore1000`（有设备并装了 SDK 时会被 `macainfo` 探测结果覆盖）；`thread_warp_size` 为 `64`。

**预期结果**：`warp` 打印 `64`，`mtriple` 打印 `mxc-metax-macahca`。若 `Target({"kind":"maca"})` 直接抛「unknown target kind maca」，说明当前 tilelang 库未以 `USE_MACA=ON` 编译，maca kind 没有被注册进 C++ 端。具体运行输出**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 CUDA kernel 里按 32 个线程一组写的 warp 划分，直接搬到 MACA 上可能不对？

**参考答案**：因为 MACA 的 `thread_warp_size` 默认是 64（见 `maca_target_kind.cc:67`），一个 warp 里有 64 个线程而非 32。warp 内的隐式同步、warp shuffle、bank conflict 的粒度都按 64 计算，按 32 写会损失一半并行度或同步语义错位。

**练习 2**：`{"kind":"maca"}` 和 `{"kind":"maca","mcpu":"xcore2000"}` 构造出的 target 有什么区别？

**参考答案**：前者会触发 canonicalizer 调 `tvm_callback_maca_get_arch` 去探测（探测不到回退 `xcore1000`）；后者因为已显式给了 `mcpu`，canonicalizer 不会覆盖它，最终 `mcpu=xcore2000`，对应不同的指令集/架构特性。

---

### 4.2 可用性检测：TileLang 如何判断 MACA 在不在

#### 4.2.1 概念说明

当你不显式指定 target（即 `target="auto"`）时，TileLang 会按注册顺序依次调用各后端的「检测器（detector）」，第一个返回非 `None` 的胜出（详见 u3-l1）。MACA 也注册了这样一个检测器，它要回答一个问题：**当前机器到底适不适合用 maca？**

检测不是简单地「有没有 MetaX 显卡」，而是一组条件：既要 MACA SDK 装了、能找到路径，又要避免和 ROCm/HIP 环境冲突。

#### 4.2.2 核心流程

MACA 检测器 `_detect_maca_target()` 的判定逻辑（短路求值）：

```
_detect_maca_target():
  ① 若 torch.version.hip is not None  ─► return None   （PyTorch 是 HIP 版，让位给 hip）
  ② 若 not check_maca_availability()  ─► return None   （找不到 MACA SDK 路径）
  ③ 否则                               ─► return Target("maca")
```

其中 `check_maca_availability()` 的判定是：能否成功调用 `mxcc.find_maca_path()` 拿到 MACA 安装路径。而 `find_maca_path()` 的判定是：环境变量 `MACA_PATH`/`MACA_HOME` 是否设置（设置了就直接用，没设置就抛异常 → 被外层 catch 成「不可用」）。

#### 4.2.3 源码精读

**检测器本体。**

> [tilelang/maca/target.py:29-37](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/target.py#L29-L37) —— `_detect_maca_target`：先看 `torch.version.hip`（HIP 版 torch 则让位），再看 `check_maca_availability()`，都通过才返回 `Target("maca")`。

> [tilelang/maca/target.py:14-26](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/target.py#L14-L26) —— `check_maca_availability`：用 try/except 包住 `mxcc.find_maca_path()`，成功则 `True`，任何异常都视作 `False`。

> [tilelang/maca/target.py:48](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/target.py#L48) —— `register_target_detector("maca", _detect_maca_target, override=True)`：把检测器挂进全局检测器表。

**「找 MACA 路径」的真正实现。**

> [tilelang/contrib/mxcc.py:191-203](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/contrib/mxcc.py#L191-L203) —— `find_maca_path`：若 `MACA_HOME` 非空则返回它，否则直接抛 `RuntimeError`，提示用户手动设置 `MACA_PATH`。

注意：这里的 `MACA_HOME` 来自 `tilelang.env`，它的取值优先级是 `MACA_PATH` → `MACA_HOME` 环境变量 → `which mxcc` 推导 → `/opt/maca` 兜底（见下）。

> [tilelang/env.py:188-198](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/env.py#L188-L198) —— `_find_maca_home`：环境变量 `MACA_PATH` 或 `MACA_HOME` 优先；否则用 `shutil.which("mxcc")` 反推；再否则试 `/opt/maca`，都不行返回空串。

**架构探测回调（被 canonicalizer 调用）。**

> [tilelang/contrib/mxcc.py:154-188](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/contrib/mxcc.py#L154-L188) —— `get_maca_arch`：执行 `$MACA_PATH/bin/macainfo`，用正则抓 `Name: XCORExxxx` 得到真实 GPU 架构；找不到则回退 `xcore1000`。

**两个判断型工具函数。**

> [tilelang/maca/target.py:40-45](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/target.py#L40-L45) —— `target_is_maca` / `target_has_async_copy`：薄封装，转调 C++ FFI `TargetIsMaca` / `TargetHasAsyncCopy`。C++ 侧 `TargetIsMaca` 在 `src/maca/op/*.cc` 里被大量用来给 MACA 专属算子做分派门控（如 `gemm.cc`、`reduce.cc`）。

#### 4.2.4 代码实践

**目标**：把检测器的判定条件「翻译」成一张可核对的清单。

**操作步骤**（源码阅读型）：

1. 打开 [tilelang/maca/target.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/target.py)，对照 `_detect_maca_target` 与 `check_maca_availability`。
2. 写出一张「auto 会选中 maca」的必要条件表：

| 条件 | 判断方式 | 不满足时 |
| --- | --- | --- |
| PyTorch 不是 HIP 版 | `torch.version.hip is None` | 让位给 hip，返回 None |
| 能找到 MACA SDK 路径 | `find_maca_path()` 不抛异常 | 返回 None |
| （间接）`MACA_PATH`/`MACA_HOME` 已设，或 `mxcc` 在 PATH，或存在 `/opt/maca` | env 探测 | `find_maca_path` 抛异常 |

3. 可选：在装了 MACA SDK 的机器上运行 `from tilelang.maca.target import check_maca_availability; print(check_maca_availability())`，核对返回值。

**预期结果**：你能复述出「HIP 版 torch 会让 MACA 让位」这一条，并解释为什么——因为同一个 GPU 在 HIP 环境下应走 `hip` target，两个检测器不能同时胜出。运行结果**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：在一台既装了 CUDA 又装了 MACA SDK 的机器上，`target="auto"` 会选中谁？

**参考答案**：会选中 `cuda`。因为 auto 检测按注册顺序遍历，顺序是 CUDA→HIP→Metal→MACA（见 u3-l1），CUDA 检测器先返回非 None 就短路了，根本轮不到 MACA。想用 MACA 必须显式 `target={"kind":"maca"}`。

**练习 2**：把 `MACA_PATH` 设成一个不存在的路径，`check_maca_availability()` 返回什么？为什么？

**参考答案**：返回 `False`。因为 `find_maca_path` 只看 `MACA_HOME` 是否非空就直接返回它（并不校验路径真实存在），但如果 `MACA_HOME` 为空则会抛异常。若你把 `MACA_PATH` 设成空串或未设、且系统也没有 `mxcc`/`/opt/maca`，就会抛异常被 catch 成 False；若设成「非空但不存在」的路径，`find_maca_path` 会返回该路径、`check_maca_availability` 返回 True，但后续真正调用 mxcc 编译时才会失败。这是一个值得注意的「检测通过 ≠ 能编译」的坑。

---

### 4.3 执行后端：生成的代码如何变成可运行件

#### 4.3.1 概念说明

回顾 u3-l2：**执行后端**决定「生成的设备代码用什么机制加载、并接到 Python 调用」。它和 target 正交，但每个 target kind 会登记一份「允许的执行后端清单」，auto 时取清单里第一个「可用」的。

MACA 在 `tilelang/maca/execution_backend.py` 里登记了 4 个执行后端：`tvm_ffi`、`mcrtc`、`cython`、`cutedsl`。其中 `tvm_ffi` 是默认且始终可用的那一个。

#### 4.3.2 核心流程

执行后端的解析（`resolve_execution_backend_spec`）逻辑：

```
给定 target (kind=maca) 和请求 (auto/指定名)
  ① 取 maca 下所有「supports_target 通过」的 spec
  ② 过滤出「is_available() 为真」的子集
  ③ 若请求是 auto ─► 取子集第一个（即 tvm_ffi）
     若请求指定名 ─► 必须在「通过 supports_target」清单里，
                       且在「可用」子集里，否则报错
```

关键：`tvm_ffi` 的 spec 没有传 `is_available`（默认 `_always_available`），并且带 `enable_host_codegen=True, enable_device_compile=True`——所以它既能生成 host 代码、又会真正编译设备代码（调 mxcc），是完整可用的默认后端。

#### 4.3.3 源码精读

**MACA 执行后端登记表。**

> [tilelang/maca/execution_backend.py:34-58](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/execution_backend.py#L34-L58) —— 依次注册：
> - `tvm_ffi`（`supports_target=_is_plain_maca_target`，`enable_host_codegen=True`，`enable_device_compile=True`）—— 默认主力；
> - `mcrtc`（`is_available=_is_mcrtc_available`）；
> - `cython`；
> - `cutedsl`（仅当 target.keys 含 `cutedsl` 时匹配）。

**「plain maca」与「cutedsl maca」的区分。**

> [tilelang/maca/execution_backend.py:8-13](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/execution_backend.py#L8-L13) —— `_is_plain_maca_target`：kind 是 `maca` 且 keys 不含 `cutedsl`；`_is_cutedsl_target`：kind 是 `maca` 且 keys 含 `cutedsl`。即同一个 maca kind，按是否带 `cutedsl` 标记走不同后端。

**解析逻辑（公共代码，非 maca 专属）。**

> [tilelang/backend/execution_backend.py:94-116](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/backend/execution_backend.py#L94-L116) —— `resolve_execution_backend_spec`：auto 时取「可用」子集的第一个；指定名时先校验在「允许清单」、再校验「可用」。第 104 行 `return allowed_available_specs[0]` 就是「auto 取首个可用」的落点。

> [tilelang/backend/execution_backend.py:36-37](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/backend/execution_backend.py#L36-L37) —— `ExecutionBackendSpec.matches`：用 `supports_target` 谓词决定某个 spec 是否适配当前 target。

> [tilelang/backend/execution_backend.py:82-83](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/backend/execution_backend.py#L82-L83) —— `allowed_backends_for_target`：列出某 target 允许的执行后端名（实践里会用到）。

> [tilelang/backend/execution_backend.py:28-34](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/backend/execution_backend.py#L28-L34) —— `ExecutionBackendSpec` 数据类：字段含 `is_available`、`supports_target`、`enable_host_codegen`、`enable_device_compile`。

**关于 `mcrtc` 的诚实说明（重要）。**
`mcrtc` 的可用性检查是 `_is_mcrtc_available`：

> [tilelang/maca/execution_backend.py:16-21](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/execution_backend.py#L16-L21) —— 它试图 `from tilelang.jit.adapter.mcrtc import is_mcrtc_available`，捕获 `ImportError` 返回 `False`。

但在当前 HEAD 的源码树里，`tilelang/jit/adapter/` 下**并不存在 `mcrtc` 子模块**（只有 `cutedsl`/`cython`/`nvrtc`/`torch` 等）。因此 `_is_mcrtc_available()` 目前**总是返回 `False`**，`mcrtc` 处于「已登记但不可用」的状态。这是 metax 分支为后续接入预留的接口，本讲如实说明，不假装它可用。

#### 4.3.4 代码实践

**目标**：列出 maca target 允许的执行后端，并确认 auto 会选哪个。

**操作步骤**（构造验证型，不依赖设备）：

```python
# 示例代码：查询 maca 允许的执行后端
from tvm.target import Target
from tilelang.backend.execution_backend import (
    allowed_backends_for_target,
    resolve_execution_backend,
)

t = Target({"kind": "maca"})
print("allowed (含不可用):", allowed_backends_for_target(t, include_unavailable=True))
print("available        :", allowed_backends_for_target(t, include_unavailable=False))
print("auto resolves to :", resolve_execution_backend(None, t))
```

**需要观察的现象**：`available` 列表里应当有 `tvm_ffi`（和 `cython`，如果其依赖满足）；`mcrtc` 一般不出现在 available 里（理由见上一节的诚实说明）；`auto resolves to` 打印 `tvm_ffi`。

**预期结果**：`resolve_execution_backend(None, t)` 返回 `"tvm_ffi"`。具体哪些出现在 available 列表取决于本机是否装了 cython 等依赖，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `tvm_ffi` 能成为 maca 的默认执行后端？

**参考答案**：因为它登记时 `is_available` 用了默认的 `_always_available`（永远为真），且带 `enable_host_codegen=True, enable_device_compile=True`，能完成从 host 代码生成到设备编译的完整链路；又排在清单首位，auto 取首个可用就选它。

**练习 2**：如果我显式 `tilelang.compile(..., execution_backend="mcrtc", target={"kind":"maca"})`，会发生什么？

**参考答案**：`mcrtc` 在「允许清单」里（通过了 `supports_target`），但不在「可用」子集里（`_is_mcrtc_available()` 为假），所以会命中 [execution_backend.py:111-115](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/backend/execution_backend.py#L111-L115) 的分支，抛出形如「Execution backend 'mcrtc' requires extra dependencies and is not available now」的错误，并提示改用可用的后端。

---

### 4.4 运行验证与常见问题排查

#### 4.4.1 概念说明

「能编译」和「能运行」是两件事。对 MACA 后端，运行需要三层都到位：

1. **tilelang 库以 `USE_MACA=ON` 编译**（C++ 端注册了 maca kind/codegen/device api）；
2. **MACA SDK + 驱动可用**（能找到 `mxcc`、`macainfo`，`MACA_PATH` 已设）；
3. **运行期动态库可达**（`LD_LIBRARY_PATH` 含 MACA 的 lib 目录）。

幸运的是，**仅查看生成的 MACA 源码**不需要第 2、3 层——只要第 1 层满足即可，因为有一条「只生成源码、不调 mxcc」的路径。

#### 4.4.2 核心流程

两条验证路径：

```
【路径 A：有设备，端到端跑通】
export MACA_PATH=/opt/maca
export LD_LIBRARY_PATH=$MACA_PATH/lib:$MACA_PATH/mxgpu_llvm/lib:$LD_LIBRARY_PATH
export PATH=$MACA_PATH/mxgpu_llvm/bin:$PATH
kernel = tilelang.compile(func, target={"kind":"maca"})
c = kernel(a, b)              # 真正在 MetaX GPU 上执行

【路径 B：无设备，只看源码】
from tilelang.engine import lower
artifact = lower(func, target={"kind":"maca"})   # enable_device_compile 默认 False
print(artifact.kernel_source)                    # 拿到 MACA 源码，全程不调 mxcc
```

路径 B 之所以可行，是因为 `lower` 默认 `enable_device_compile=False`，会走 `device_codegen_without_compile`：

> [tilelang/engine/lower.py:348-393](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/engine/lower.py#L348-L393) —— `lower` 函数：第 370 行 `codegen_mod = device_codegen(...) if enable_device_compile else device_codegen_without_compile(...)`。默认 `enable_device_compile=False` → 走「不编译」分支，第 371 行 `kernel_source = codegen_mod.inspect_source()` 取出源码，存入返回的 `CompiledArtifact.kernel_source`。

> [tilelang/engine/lower.py:305-307](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/engine/lower.py#L305-L307) —— `device_codegen_without_compile`：转调 `resolve_device_codegen(target).lower(..., compile_device=False)`，对 maca 即 `tilelang_maca_without_compile`。

而完整的 `tilelang.compile`（路径 A）由执行后端 `tvm_ffi` 的 `enable_device_compile=True` 驱动，会真正调用 mxcc 把源码编成 mcbin 再加载。

#### 4.4.3 源码精读（运行期环境与验证命令）

**运行期需要导出的环境变量。**

> [docs/get_started/Installation_maca.md:116-127](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/get_started/Installation_maca.md#L116-L127) —— 官方验证步骤：导出 `MACA_PATH`、把 `$MACA_PATH/lib` 与 `$MACA_PATH/mxgpu_llvm/lib` 加入 `LD_LIBRARY_PATH`、把 `$MACA_PATH/mxgpu_llvm/bin` 加入 `PATH`，然后 `python -c "import tilelang"` 与跑 `examples/quickstart.py` 验证。

**mxcc 编译器路径。**

> [tilelang/contrib/mxcc.py:359-362](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/contrib/mxcc.py#L359-L362) —— `get_mxcc_compiler`：返回 `$MACA_HOME/mxgpu_llvm/bin/mxcc`。若 `MACA_HOME` 没设对，这里就会指向错误路径，编译阶段报「找不到 mxcc」。

**mxcc 实际编译调用。**

> [tilelang/contrib/mxcc.py:19-110](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/contrib/mxcc.py#L19-L110) —— `compile_maca`：把生成的 MACA 源码写成临时 `.maca` 文件，调用 `mxcc -x maca -device-obj -O3 -lineinfo --offload-arch=<arch> ... -o <out>`，产物默认 `mcbin`。第 102-104 行在返回码非 0 时把「源码 + 编译错误 + 完整命令」抛出——这是排查编译失败的第一手信息。

#### 4.4.4 代码实践

**目标**：在无 MetaX 设备的机器上，依然拿到一份用 maca target 生成的 GEMM 源码。

**操作步骤**（源码生成型实践，前提：tilelang 以 `USE_MACA=ON` 编译）：

```python
# 示例代码：用 maca target 生成（但不编译）一个 GEMM 的设备源码
import tilelang.language as T
from tilelang.engine import lower

@T.prim_func
def gemm(A: T.Tensor((1024, 1024), "float16"),
         B: T.Tensor((1024, 1024), "float16"),
         C: T.Tensor((1024, 1024), "float16")):
    with T.Kernel(8, 8, threads=128) as (bx, by):
        A_sh = T.alloc_shared((128, 32), "float16")
        B_sh = T.alloc_shared((32, 128), "float16")
        C_lc = T.alloc_fragment((128, 128), "float32")
        T.clear(C_lc)
        for k in T.Pipelined(32, num_stages=3):
            T.copy(A[by * 128, k * 32], A_sh)
            T.copy(B[k * 32, bx * 128], B_sh)
            T.gemm(A_sh, B_sh, C_lc)
        T.copy(C_lc, C[by * 128, bx * 128])

artifact = lower(gemm, target={"kind": "maca"})
print(artifact.kernel_source[:1500])   # 只打印前 1500 字符即可
```

**需要观察的现象**：打印出的源码是 MACA 风格的设备代码（包含 `__global__` 入口、可能含 `__mtma`/`__mma` 之类的 MACA 张量核 intrinsic、warp 相关内建）。整个过程**不会**调用 mxcc，所以即便机器上没有 MACA SDK 也能跑通这一步。

**预期结果**：`artifact.kernel_source` 是一段非空的 MACA C++ 设备源码。若抛 `No device codegen registered for target 'maca'`，说明库未以 `USE_MACA=ON` 编译。源码具体内容**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：报错「Failed to automatically detect MACA installation. Please set the MACA_PATH...」出现在哪一步？怎么解？

**参考答案**：出现在 `mxcc.find_maca_path()`（[mxcc.py:191-203](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/contrib/mxcc.py#L191-L203)），当 `MACA_HOME` 为空时抛出。解决办法：`export MACA_PATH=/opt/maca`（指向你机器上真实的 MACA SDK 根目录）。

**练习 2**：`import tilelang` 成功，但 `tilelang.compile(..., target={"kind":"maca"})` 在设备编译阶段失败。最该先检查什么？

**参考答案**：先检查运行期环境三件套——`MACA_PATH` 是否设、`LD_LIBRARY_PATH` 是否含 `$MACA_PATH/lib` 与 `$MACA_PATH/mxgpu_llvm/lib`、`PATH` 是否含 `$MACA_PATH/mxgpu_llvm/bin`（即 `mxcc` 能否被找到）。其次看抛错里附带的「Command: ...」与编译输出，定位是 mxcc 缺失还是源码语法问题。注意 `import tilelang` 只证明 Python 包和 `libtilelang.so` 加载成功，**不代表** MACA SDK/驱动就绪。

## 5. 综合实践

把本讲四个模块串起来，完成下面这个端到端小任务（对应本讲规格里的实践要求）：

**任务**：阅读 `tilelang/maca/target.py`，写出 MACA target 在 auto 检测中被选中的完整条件；然后用 `target={"kind":"maca"}` 走一遍 GEMM，有设备就跑、无设备就打印生成的源码。

**步骤**：

1. **写检测条件**。打开 [tilelang/maca/target.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/target.py)，用自己的话写出 `_detect_maca_target` 的三个判定分支（HIP 让位 → SDK 可用性 → 返回 Target）。再追到 [mxcc.py 的 find_maca_path](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/contrib/mxcc.py#L191-L203)，说明「SDK 可用性」最终落到哪个环境变量。

2. **准备 GEMM**。直接用 [examples/gemm/example_gemm.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm.py#L5-L26) 里那个 `matmul` kernel（它用 `@tilelang.jit` + `.compile(...)`）。

3. **分两种情况执行**：
   - **有 MetaX 设备**：按 [Installation_maca.md:116-127](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/get_started/Installation_maca.md#L116-L127) 导出三个环境变量，把 `matmul.compile(...)` 改成带 `target={"kind":"maca"}`，运行并 `print(kernel.get_kernel_source())`，再 `kernel(a,b)` 验证数值。
   - **无 MetaX 设备**：用 4.4.4 的 `lower(gemm, target={"kind":"maca"})` 路径，打印 `artifact.kernel_source`，确认拿到的是 MACA 风格源码（看到 warp=64 相关或 MACA intrinsic 即可）。

4. **记录观察**：在一份 Markdown 里记下——(a) auto 选中 maca 的条件；(b) `resolve_execution_backend(None, Target({"kind":"maca"}))` 的结果；(c) 生成源码里能体现「这是 maca 而非 cuda」的一处特征（例如张量核 intrinsic 前缀、warp_size 推断）。

**自检**：如果你能说清楚「为什么 `import tilelang` 通过、却仍可能在 compile 时报 mxcc 找不到」，本讲就过关了。

## 6. 本讲小结

- `maca` 是与 `cuda`/`hip` 平级的一等 target kind，C++ 侧在 `maca_target_kind.cc` 注册，默认 `thread_warp_size=64`，并由 canonicalizer 自动补齐 `mtriple`/`mcpu`。
- MACA 的 target 检测器 `_detect_maca_target` 有两道关卡：PyTorch 是 HIP 版则让位、`check_maca_availability`（即能否找到 MACA SDK 路径）通不过则放弃。
- 「找到 MACA 路径」最终落到环境变量 `MACA_PATH`/`MACA_HOME`（或 `mxcc` 在 PATH、或 `/opt/maca` 兜底）。
- MACA 登记了 4 个执行后端（tvm_ffi/mcrtc/cython/cutedsl），`tvm_ffi` 因始终可用且能完整编译而成为默认；`mcrtc` 在当前源码树里是「已登记但不可用」的预留接口。
- 设备代码生成分两条：`tilelang_maca`（调 mxcc 编译）与 `tilelang_maca_without_compile`（只出源码）；后者让「无设备也能查看生成的 MACA 源码」成为可能。
- 运行期需要 `MACA_PATH` + `LD_LIBRARY_PATH` + `PATH` 三件套；`import tilelang` 成功不等于 MACA 后端就绪。

## 7. 下一步学习建议

- 想看 MACA 后端「整条全栈」如何拼起来，进入专家层 [u7-l1（MACA 后端架构总览）](u7-l1-maca-backend-overview.md)，那里会讲 device API、module 加载与 Python/C++ 注册的对应关系。
- 对 MACA 如何生成张量核指令（mfma）感兴趣，接着读 [u7-l3（MACA MMA intrinsics）](u7-l3-maca-mfma-intrinsics.md)。
- 想理解三后端（CUDA/ROCm/MACA）在 warp_size、triple、MMA 命名上的系统性差异，看 [u7-l5（MACA vs CUDA vs ROCm 差异对比）](u7-l5-maca-vs-cuda-vs-rocm.md)。
- 若你想自己加一个新后端，[u9-l1（扩展：新增目标后端）](u9-l1-add-new-backend.md) 会以 MACA 为模板讲解注册机制。
