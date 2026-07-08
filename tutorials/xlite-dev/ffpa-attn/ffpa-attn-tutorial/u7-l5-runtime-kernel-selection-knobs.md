# 运行时 kernel 选择开关

## 1. 本讲目标

本讲是「手写 CUDA 后端与构建系统」单元的收尾篇，承接 u7-l4 讲过的构建期变量（`FFPA_BUILD_ARCH` / `ENABLE_FFPA_ALL_STAGES` / `ENABLE_FFPA_ALL_HEADDIM` / `FFPA_DEV_HEADDIMS`），专门回答一个问题：

> `env.py` 里那一长串 `ENABLE_FFPA_*` 开关，到底哪些是「运行期」、哪些是「构建期」？它们如何影响 kernel 的实际行为？

读完本讲你应当能够：

1. **区分构建期与运行期**：能精确说出 `acc` / `stages` 是逐调用可变的真运行期参数，而 `FORCE_QK_F16` / `SMEM_SWIZZLE_*` / `PERSIST_*` 等是被「烘焙」进 `_C` 的构建期宏。
2. **掌握 MMA acc 精度语义**：理解 `acc` 运行期参数（0=fp16、1=fp32）与 `ENABLE_FFPA_FORCE_QK_F16` / `ENABLE_FFPA_FORCE_PV_F16` 两个构建期细化开关的关系、互斥推荐与 bf16 必须 fp32 的硬件约束。
3. **理解 launch grid 布局**：知道 `ENABLE_FFPA_LAUNCH_GRID_DNHB` 想把网格从 `grid(N/Br, B*H)` 改成 `grid(N/Br, H, B)`，并能识别出当前实现里的一个宏拼写问题。
4. 把全部「kernel 选择」开关分成「MMA 精度 / 预取·共享 / swizzle / persist / 流水线·网格」五类，对每类举一例说明开关后 kernel 行为如何变化。

---

## 2. 前置知识

本讲默认你已经学完：

- **u7-l1**：手写 CUDA 前向 kernel 的三套模板（大 D 走 Split-D、小 D 走 persistent、decode 走 stage1/stage2），以及 `g2s`（global→SMEM）/ `s2r`（SMEM→寄存器）/ `mma.sync` 流水线。
- **u7-l2**：SRAM/寄存器复杂度（Split-D 使 SRAM 复杂度 O(1) in D、寄存器 O(d/4)），swizzle 消 bank 冲突 vs padding 补列的取舍。
- **u7-l3**：per-head_dim 代码生成与 C++ 三级分发链（统一入口 → `(dtype,acc)` 入口 → per-head_dim 符号 → `launch_ffpa_attn_fwd_template`），以及 `acc` 编码 0=f16 / 1=f32。
- **u7-l4**：`ENV` 类作为「构建期总指挥」决定编什么、怎么编、为哪些 SM 编。

需要重新强调的几个术语：

| 术语 | 含义 |
|---|---|
| **构建期（build-time）** | `pip install .` / `python setup.py build_ext` 编译 `_C` 扩展的时刻。值被「冻结」进 `.so`。 |
| **运行期（runtime）** | Python 进程 import `ffpa_attn` 或调用 `ffpa_attn_func` 的时刻。 |
| **MMA acc** | 矩阵乘（`Q@K^T`、`P@V`）累加器的精度。fp16 输入可选 fp16/fp32，bf16 输入强制 fp32。 |
| **`-D` 宏** | nvcc 的预处理器宏定义。`env_cuda_cflags()` 把开关译成 `-DENABLE_FFPA_XXX`，kernel 里用 `#ifdef` 消费。 |
| **constexpr 配置函数** | launch_templates.cuh 里形如 `getConfigXXX()` 的 `constexpr` 函数，编译期根据宏与 `kHeadDim` 选定 tile/stage/pad/persist。 |

> ⚠️ 一个本讲会反复强调的核心事实：docs/env.md 把一大类开关称为「runtime kernel-selection」，但**对 CUDA 后端而言，它们其实是在编译 `_C` 时被快照成 `-D` 宏的**——要真正改变 kernel 行为必须重新编译 `_C`。真正逐调用可变、无需任何重编的只有 `stages` 与 `acc` 两个 int 参数。这个区别是本讲最重要的认知。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [env.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py) | `ENV` 类：在 Python import 时读取所有 `ENABLE_FFPA_*` / `FFPA_*` 环境变量，提供 `enable_*()` 类方法、`env_cuda_cflags()`（把它们译成 `-D` 宏）、`list_ffpa_env()`（打印当前取值）。 |
| [docs/env.md](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/docs/env.md) | 所有环境变量的权威文档，按「build-time / runtime kernel-selection」两节组织。 |
| [csrc/cuffpa/launch_templates.cuh](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/launch_templates.cuh) | host 端 launcher：用一组 `getConfigXXX()` `constexpr` 函数在编译期把 `#ifdef` 宏 + `kHeadDim` 翻译成 tile/stage/pad/persist/swizzle 等模板参数。 |
| [csrc/cuffpa/ffpa_attn_api.cc](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_api.cc) | 统一 pybind 入口 `ffpa_attn_forward`：按 `dtype` 与运行期 `acc` 分发到 `fp16f16` / `fp16f32` / `bf16f32` 符号。 |
| [src/ffpa_attn/cuda/__init__.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cuda/__init__.py) | 把 CUDA 前向注册成 `torch.ops.ffpa_attn._fwd_cuda`，签名里 `stages` / `acc` / `tma` 是运行期 int 参数。 |
| [src/ffpa_attn/cuda/_ffpa_fwd.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cuda/_ffpa_fwd.py) | `_ffpa_attn_forward_cuda` 的默认值（`stages=2, acc=1, tma=0`）。 |
| [src/ffpa_attn/functional.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py) | `CUDABackend` 配置类：用户面 `acc` / `stages` 字段，`acc_code` 属性把它们映射成运行期 int 并在前向调用点传入。 |

---

## 4. 核心概念与源码讲解

### 4.1 ENV 类：env.py 如何在 import 时读取并暴露 `ENABLE_FFPA_*`

#### 4.1.1 概念说明

`ENV` 是一个纯类（不实例化，全用 `@classmethod`），它把「读环境变量」这件事集中在仓库根目录的 `env.py` 里。关键点是 **读取时机**：每个类属性在 Python **首次 import env.py 时**被求值一次（`os.environ.get(...)`），之后整个进程里 `ENV.ENABLE_FFPA_XXX` 就固定了。

这意味着：

- 在 Python 进程内，`ENV` 反映的是「启动进程时」的环境变量值；进程启动后再 `export ENABLE_FFPA_XXX=1` 不会改变已 import 的 `ENV`。
- `ENV` 的取值有**两个消费者**：一是构建期 `env_cuda_cflags()`（译成 `-D` 宏编进 `_C`），二是任何想查询当前配置的 Python 代码（如 `list_ffpa_env()` 打印）。

#### 4.1.2 核心流程

```text
启动 Python 进程
   │  os.environ 里带着 ENABLE_FFPA_*=0/1
   ▼
import env  ──► ENV 类属性被求值（一次性快照）
   │              ENABLE_FFPA_FORCE_QK_F16 = bool(int(os.environ.get(...,0)))
   │              ENABLE_FFPA_SMEM_SWIZZLE_Q = bool(int(...))
   │              ...
   ▼
两个消费者：
  (A) 构建期：env_cuda_cflags()  ─► ['-DENABLE_FFPA_FORCE_QK_F16', ...] ─► nvcc ─► _C.so（冻结）
  (B) 运行期：ENV.enable_smem_swizzle_q() / list_ffpa_env()（仅查询/打印）
```

#### 4.1.3 源码精读

所有运行期「kernel 选择」开关都是 `bool(int(os.environ.get(NAME, DEFAULT)))` 的类属性，默认值已在注释里写清：

- MMA 精度：[env.py:39-49](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L39-L49) 定义 `FORCE_QK_F16` / `FORCE_PV_F16`，默认都为 `False`。
- 预取/共享：[env.py:51-60](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L51-L60) 定义 `PREFETCH_QKV`（默认 True）、`QKV_SMEM_SHARE`（默认 False）。
- swizzle：[env.py:62-80](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L62-L80) 定义 `SMEM_SWIZZLE_{Q,K,V}`，默认全 True。
- persist：[env.py:82-106](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L82-L106) 定义 `PERSIST_{Q_G2S,KV_G2S,Q_S2R,V_S2R}`。
- 流水线/网格：[env.py:108-116](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L108-L116) 定义 `REGISTERS_PIPE_KV`、`LAUNCH_GRID_DNHB`。

每个属性都配一个 `enable_xxx()` 类方法做查询入口，例如 [env.py:224-234](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L224-L234) 的 swizzle 三件套；其中 `enable_persist_v_s2r()` 还额外体现了依赖关系——只有 `persist_kv_g2s` 开启时它才可能为真：

```python
@classmethod
def enable_persist_v_s2r(cls):
    if cls.enable_persist_kv_g2s():
        return cls.ENABLE_FFPA_PERSIST_V_S2R
    return False
```

而 `list_ffpa_env()`（[env.py:336-375](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L336-L375)）把所有开关连同「该设成什么」打印出来，是排查「我到底编了/跑的是什么配置」的第一工具。

#### 4.1.4 代码实践

实践目标：用 `list_ffpa_env()` 看清当前进程实际生效的开关取值。

操作步骤：

1. 在仓库根目录执行 `python3 env.py`（文件末尾 `if __name__ == "__main__": ENV.list_ffpa_env()`，见 [env.py:881-883](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L881-L883)）。
2. 再用环境变量覆盖一两个开关重跑，例如 `ENABLE_FFPA_FORCE_QK_F16=1 ENABLE_FFPA_QKV_SMEM_SHARE=1 python3 env.py`。

需要观察的现象：每次输出的 `ENABLE_FFPA_*` 行会显示当前值与对应的 `export NAME=值` 命令；被覆盖的开关值应随之翻转。

预期结果：默认输出里 `FORCE_QK_F16=0`、`PREFETCH_QKV=1`、`SMEM_SWIZZLE_Q=1`、`LAUNCH_GRID_DNHB=0` 等；覆盖后对应行翻转。注意 `list_ffpa_env` 只反映「当前进程读到的值」，**并不告诉你 `_C` 当初是用什么值编的**——这是下一节的要点。

> 若无 GPU 或未装 torch，`python3 env.py` 可能在 `get_build_arch_list` 处不报错（它只在调用时才查设备），但 `list_ffpa_env` 里那行会触发设备查询；遇到报错可只读属性部分，结论一致。

#### 4.1.5 小练习与答案

**练习 1**：为什么在已运行的 Python 进程里 `os.environ['ENABLE_FFPA_SMEM_SWIZZLE_Q']='0'` 后再调用 `ENV.enable_smem_swizzle_q()` 仍返回 True？

**答案**：`ENV` 的类属性在 `import env` 时一次性求值，之后不再读 `os.environ`。要改值必须在启动进程**之前** `export`，或重新启动进程。

**练习 2**：`list_ffpa_env()` 打印的 `ENABLE_FFPA_SMEM_SWIZZLE_Q=1` 是否等价于「当前 CUDA kernel 用了 swizzle」？

**答案**：不等价。`list_ffpa_env` 只反映当前进程读到的环境变量值；CUDA kernel 用的是 `_C` 编译时快照的 `-D` 宏。二者可能不一致（例如 `_C` 是别人用旧环境编好的 wheel）。

---

### 4.2 构建期 vs 运行期：两条消费路径的精确边界

#### 4.2.1 概念说明

这是本讲最关键的认知。`env.py` 读到的同一个 `ENABLE_FFPA_*` 值，有**两条消费路径**，决定了它是「构建期」还是「运行期」：

1. **构建期路径（绝大多数开关）**：`env_cuda_cflags()`（[env.py:274-327](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L274-L327)）把 `ENV.enable_xxx()` 的布尔结果翻译成 nvcc 的 `-DENABLE_FFPA_XXX` 宏；这些宏在 `csrc/cuffpa/launch_templates.cuh` 里被 `#ifdef` 包裹，经 `constexpr` 函数在**编译 `_C` 时**固化成模板参数。改了它就必须重编 `_C`。

2. **运行期路径（只有 `stages` 和 `acc`）**：这两个值不走 `-D` 宏，而是作为普通 `int` 函数参数，从前向调用点一路传到 kernel launcher，**每次调用都可不同**，无需重编。

docs/env.md（key-note #2，[docs/env.md:63-70](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/docs/env.md#L63-L70)）把除 `ALL_STAGES` / `ALL_HEADDIM` 之外的开关都归为「runtime kernel-selection ... can be toggled without rebuilding」。这一说法需要精确理解：它指的是**这些开关不像 `ALL_STAGES` / `ALL_HEADDIM` 那样改变「生成哪些翻译单元」**（codegen 层面），它们只在已生成的 TU 内翻转 `#ifdef` 分支；但对 CUDA 后端而言，翻转 `#ifdef` 分支仍需重新编译 `_C`。所以更准确的表述是：

> 这些开关由 `env.py` 在 import 时读取、在编译 `_C` 时快照成 `-D` 宏；要让 CUDA kernel 真正改变行为，需要重新编译 `_C`。真正「逐调用可变、零重编」的只有 `stages` 与 `acc`。

#### 4.2.2 核心流程

```text
                     ┌─────────── ENV.enable_xxx() 读到的 bool ───────────┐
                     │                                                    │
        (路径 A) 构建期                                                      (路径 B) 运行期
        env_cuda_cflags()                                                   CUDABackend.acc / stages
        append "-DENABLE_FFPA_XXX"                                          (functional.py)
              │                                                                   │
              ▼                                                                   ▼
        nvcc 编译 _C.so                                                     _ffpa_attn_forward_cuda(..., stages, acc, ...)
        launch_templates.cuh 里 #ifdef → constexpr                        torch.ops.ffpa_attn._fwd_cuda(..., stages, acc, ...)
        值被冻结进 .so                                                       每次调用都可变，不重编
```

构建期开关的「冻结」是字面意义的：`getConfigPadQ()` 这类 `constexpr` 函数在 [launch_templates.cuh:172-179](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/launch_templates.cuh#L172-L179) 里，`#ifdef ENABLE_FFPA_SMEM_SWIZZLE_Q` 决定 `kPadQ = 0`（swizzle）还是 `kPadQ = 8`（padding）——这是编译期常量，编完后 `.so` 里只会剩下其中一条分支。

```cpp
static constexpr int getConfigPadQ() {
#ifdef ENABLE_FFPA_SMEM_SWIZZLE_Q
  constexpr int kPadQ = 0;      // swizzle：零 SMEM 浪费
#else
  constexpr int kPadQ = 8;      // padding：补 8 列消 bank 冲突，多吃 SRAM
#endif
  return kPadQ;
}
```

#### 4.2.3 源码精读

**路径 A（构建期快照）**：`env_cuda_cflags()` 把每个开关逐条译成宏，例如 MMA 精度两条见 [env.py:281-284](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L281-L284)：

```python
if cls.enable_force_qk_fp16():
    extra_env_cflags.append("-DENABLE_FFPA_FORCE_QK_F16")
if cls.enable_force_pv_fp16():
    extra_env_cflags.append("-DENABLE_FFPA_FORCE_PV_F16")
```

这些 `-D` 经 `get_build_cuda_cflags()`（[env.py:760-791](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L760-L791)，其中 `extra_cuda_cflags.extend(ENV.env_cuda_cflags())` 在第 774 行）进入 nvcc 命令行。

**路径 B（真运行期）**：CUDA 前向注册的算子签名把 `stages` / `acc` / `tma` 列为 `int` 参数，见 [src/ffpa_attn/cuda/__init__.py:22-45](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cuda/__init__.py#L22-L45)；这两个值在分发层前向调用点从 `CUDABackend` 取出并传入，见 [functional.py:778-786](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L778-L786)：

```python
O, lse = _ffpa_attn_forward_cuda(
    q, k, v,
    attn_bias,
    forward_meta.stages,      # 运行期 int，逐调用可变
    forward_meta.acc_code,    # 运行期 int（0=f16, 1=f32）
    int(meta.attn_meta.is_causal),
    ...
)
```

C++ 端 `ffpa_attn_forward` 再按运行期 `acc` 分发（详见 4.3）。注意 `tma` 参数虽然一路传递，但在 C++ 入口被刻意丢弃并强制为 0，见 [ffpa_attn_api.cc:38-42](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_api.cc#L38-L42)（注释说明 legacy TMA dispatch 已移除，参数仅为 API 兼容保留）。

#### 4.2.4 代码实践

实践目标：亲手验证「路径 A 需重编、路径 B 不需重编」。

操作步骤：

1. 找到 `CUDABackend` 的 `acc` / `stages` 字段定义 [functional.py:154-160](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L154-L160)。注意 `stages` 默认随架构变：`4 if _is_hopper_or_later() else 3`。
2. 用 `CUDABackend(acc="f16")` 与 `CUDABackend(acc="f32")` 各跑一次前向（需已编译 `_C`），比较二者输出与耗时——这不需要重编 `_C`，因为 `acc` 是运行期参数。
3. 反之，把 `ENABLE_FFPA_SMEM_SWIZZLE_Q=0` 后重新 import，会发现 kernel 行为不变（除非重编 `_C`）。

需要观察的现象：第 2 步两次调用的结果应数值接近但 `acc="f16"` 通常略快/精度略低；第 3 步翻转 swizzle 环境变量但**不重编**时，TFLOPS / 输出不应改变。

预期结果：证实 `acc` 在运行期生效，swizzle 在构建期生效。

> 本实践需要编译 `_C`（`ENABLE_FFPA_CUDA_IMPL=1`）且有支持的大 D（如 D=512）形状才走 CUDA 前向；环境不具备时记为「待本地验证」，可改成纯阅读 [functional.py:778-786](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L778-L786) 与 [launch_templates.cuh:172-179](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/launch_templates.cuh#L172-L179) 两段代码并口头论证。

#### 4.2.5 小练习与答案

**练习 1**：把 `ENABLE_FFPA_FORCE_PV_F16` 从 0 改成 1，对**默认 Triton-only 安装**（没有 `_C`）的前向速度有影响吗？

**答案**：没有。这些 `ENABLE_FFPA_*` kernel 选择开关是**手写 CUDA 后端专用**的，全部走 `-D` 宏进入 `_C`。Triton-only 安装根本不编译 `_C`，开关对 Triton kernel 无任何作用。

**练习 2**：为什么 docs/env.md 仍把 `FORCE_PV_F16` 这类称为「runtime」开关，却又说改 `ALL_STAGES` 必须重编？

**答案**：docs 的区分轴是「是否改变生成的翻译单元集合」：`ALL_STAGES` / `ALL_HEADDIM` 改变 codegen 出的 `.cu` 文件与模板实例化次数（必重编且改变文件集）；其余开关只翻转已有 TU 内的 `#ifdef` 分支，docs 作者因此把它们归为「kernel-selection」。但严格说，对 CUDA 后端它们仍需重编 `_C` 才生效——这是 docs 措辞与代码实现的微妙落差，本讲把它点破。

---

### 4.3 MMA 累加器精度开关：`acc` 运行期参数 + `FORCE_QK_F16` / `FORCE_PV_F16` 构建期细化

#### 4.3.1 概念说明

FFPA 前向有两次矩阵乘：`Q@K^T`（算 score）和 `P@V`（算输出）。每次乘法的累加器精度（MMA acc dtype）可独立选择 fp16 或 fp32，组合出三种「入口」：

| 入口符号 | 激活 dtype | `Q@K^T` acc | `P@V` acc | 来源 |
|---|---|---|---|---|
| `ffpa_attn_fwd_fp16f16_d{D}` | fp16 | f16 | f16 | 固定（最省、精度最低） |
| `ffpa_attn_fwd_fp16f32_d{D}` | fp16 | 默认 f32，可被 `FORCE_QK_F16` 降为 f16 | 默认 f32，可被 `FORCE_PV_F16` 降为 f16 | 受两个宏细化 |
| `ffpa_attn_fwd_bf16f32_d{D}` | bf16 | f32（强制） | f32（强制） | bf16 无 f16-acc PTX |

关键有三层：

1. **运行期 `acc` 参数**先做大选：fp16 输入时 `acc=0` 选 `fp16f16`，`acc=1` 选 `fp16f32`；bf16 输入强制 `acc=1` 选 `bf16f32`。
2. **构建期 `FORCE_QK_F16` / `FORCE_PV_F16`** 再对 `fp16f32` 入口做**精细化**：把两次乘法中的一次降回 fp16，得到「混合精度」`fp16f32`。这就是 docs（[docs/env.md:35-38](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/docs/env.md#L35-L38)）说的 mixed mode。
3. **互斥推荐**：docs（key-note #5，[docs/env.md:69](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/docs/env.md#L69)）说二者「mutually exclusive ... enable at most one」——**同时开两个会让两次乘法都变 f16，等价退回 `fp16f16` 入口**，失去混合精度意义。

> 注意：`env_cuda_cflags()` 里**并没有** assert 强制 `FORCE_QK_F16` 与 `FORCE_PV_F16` 互斥（不像 persist 那组有显式断言）。「互斥」是文档级的推荐语义，不是构建期硬错误；同时开两个不会报错，只是结果与 `acc=0` 雷同。

#### 4.3.2 核心流程

```text
用户调用 ffpa_attn_func(forward_backend=CUDABackend(acc="f32"))
   │  acc_code = 1
   ▼ 运行期
_fwd_cuda(..., stages, acc=1, ...)
   ▼
ffpa_attn_forward(dtype, acc=1)        [ffpa_attn_api.cc]
   │  dtype=fp16, acc=1  ──► ffpa_attn_fwd_fp16f32_d{D}
   │  dtype=bf16, acc=1  ──► ffpa_attn_fwd_bf16f32_d{D}（强制 f32 acc）
   ▼ 进入 fp16f32 TU（构建期宏已冻结）
kMmaAccFloat32QK = #ifdef FORCE_QK_F16 ? 0 : 1
kMmaAccFloat32PV = #ifdef FORCE_PV_F16 ? 0 : 1
   ▼
launch_ffpa_attn_template<..., kMmaAccFloat32QK, kMmaAccFloat32PV, ...>
```

#### 4.3.3 源码精读

**用户面 `acc`**：`CUDABackend` 用字符串字段表达，`acc_code` 属性把它映射成 int（[functional.py:158-171](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L158-L171)）：

```python
name: str = "cuda"
acc: str = "f32"
stages: int = 4 if _is_hopper_or_later() else 3

def __post_init__(self, ...):
    ...
    assert self.acc in ("f16", "f32"), ...
    ...
@property
def acc_code(self) -> int:
    return _ACC_F32 if self.acc == "f32" else _ACC_F16   # f32=1, f16=0
```

bf16 输入配 `acc="f16"` 会在分发层被拒（[functional.py:579-585](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L579-L585)），与 C++ 端的硬约束一致。

**C++ 端运行期分发**：`ffpa_attn_forward` 按 dtype 与 `acc` 选符号，并强制 `tma_i=0`（[ffpa_attn_api.cc:27-77](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_api.cc#L27-L77)）。`acc` 编码注释见第 27 行 `// acc encoding: 0=f16, 1=f32.`；bf16 分支在第 65-73 行强制 `acc==1` 否则抛异常。

**构建期宏细化**：`fp16f32` 翻译单元在生成时把两个宏翻译成 `kMmaAccFloat32{QK,PV}` 常量，见 [env.py:606-617](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L606-L617)（`_render_per_headdim_fp16_tu` 的 `f32_prefix`）：

```python
f32_prefix = [
  "#ifdef ENABLE_FFPA_FORCE_QK_F16",
  "  constexpr int kMmaAccFloat32QK = 0;",   # 降级 QK acc 到 f16
  "#else",
  "  constexpr int kMmaAccFloat32QK = 1;",   # 保持 f32
  "#endif",
  "#ifdef ENABLE_FFPA_FORCE_PV_F16",
  "  constexpr int kMmaAccFloat32PV = 0;",   # 降级 PV acc 到 f16
  "#else",
  "  constexpr int kMmaAccFloat32PV = 1;",
  "#endif",
]
```

对比之下，`fp16f16` 入口直接把两个常量都写死为 0，`bf16f32` 入口都写死为 1（[env.py:602-605](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L602-L605) 与 [env.py:644-647](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L644-L647)），所以这两个宏**只对 fp16 输入 + acc=f32 有意义**。

#### 4.3.4 代码实践

实践目标：在「不重编 `_C`」的前提下，比较 `acc="f16"` 与 `acc="f32"` 的精度与速度差异。

操作步骤：

1. 编译 `_C`：`ENABLE_FFPA_CUDA_IMPL=1 pip install -e . --no-build-isolation`（参见 u7-l3）。
2. 构造 fp16 的 `B=1,H=32,N=8192,D=512` 输入，分别用 `forward_backend=CUDABackend(acc="f16")` 与 `CUDABackend(acc="f32")` 调用 `ffpa_attn_func`。
3. 用 SDPA 参考输出算 `max_abs_err`，并用 `torch.cuda.Event` 测两路耗时。

需要观察的现象：`acc="f16"` 通常更快但 `max_abs_err` 更大；`acc="f32"` 更慢但更接近 SDPA。

预期结果：证实 `acc` 是运行期参数——两次调用共用同一份 `_C.so`，无需重编即生效。若 GPU 不支持或形状回退到 SDPA/Triton，则记为「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：同时设置 `ENABLE_FFPA_FORCE_QK_F16=1` 和 `ENABLE_FFPA_FORCE_PV_F16=1` 会怎样？

**答案**：`fp16f32` 入口的 `kMmaAccFloat32QK` 与 `kMmaAccFloat32PV` 都变成 0，等价于 `fp16f16` 入口（纯 fp16 acc）。不会报错（无 assert），但失去「混合精度」意义，属于冗余配置——docs 因此推荐「至多开一个」。

**练习 2**：为什么 bf16 输入时 `FORCE_QK_F16` / `FORCE_PV_F16` 完全无效？

**答案**：bf16 走的是 `bf16f32` 入口，其两个常量在 [env.py:644-647](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L644-L647) 被硬编码为 1（强制 f32），根本不读这两个宏。硬件上不存在 bf16-acc 的 MMA PTX，所以三级（Python `__post_init__`、C++ `ffpa_attn_forward`、codegen）一致强制 bf16 用 f32 acc。

---

### 4.4 prefetch / swizzle / persist / pipeline / launch 五类开关

#### 4.4.1 概念说明

除 MMA 精度外，docs/env.md（[docs/env.md:40-61](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/docs/env.md#L40-L61)）把其余运行期 kernel 选择开关分成四组，本讲加上 MMA 精度共五类。它们全部走 4.2 的「路径 A」（构建期 `-D` 宏 → `#ifdef` `constexpr`），逐个对应 launch_templates.cuh 里一个 `getConfigXXX()` 函数：

| 类别 | 开关 | 默认 | 对应 getConfig | 行为变化（开 → 关 或反之） |
|---|---|---|---|---|
| **MMA 精度** | `FORCE_QK_F16` / `FORCE_PV_F16` | 0/0 | （在 TU 内联，见 4.3） | f32 acc → 混合精度 |
| **预取/共享** | `PREFETCH_QKV` | 1 | `getConfigPrefetchQKV` | 在恰当时机预取 QKV，+5~10% |
| | `QKV_SMEM_SHARE` | 0 | `getConfigShareSmemQKV` | Q/K/V 共用 SMEM 缓冲，省 SRAM 换重叠 |
| **swizzle** | `SMEM_SWIZZLE_{Q,K,V}` | 1/1/1 | `getConfigPad{Q,K,V}` | swizzle（kPad=0）↔ padding（kPad=8） |
| **persist** | `PERSIST_Q_G2S` | 1 | `getConfigPersistQg2s` | Q 常驻 SMEM（D≤320） |
| | `PERSIST_KV_G2S` | 1 | （多处 `#ifdef`） | KV 常驻 SMEM（D≤256），切 FA 风格 tiling |
| | `PERSIST_Q_S2R` | 0 | `getConfigPersistQs2r` | Q 常驻寄存器（D<512），省 IO 换寄存器 |
| | `PERSIST_V_S2R` | 1 | `getConfigPersistVs2r` | V 常驻寄存器（仅小 D kernel） |
| **流水线/网格** | `REGISTERS_PIPE_KV` | 0 | `getConfigRegistersPipeKV` | 寄存器乒乓双缓冲，ldmatrix 与 MMA 重叠 |
| | `LAUNCH_GRID_DNHB` | 0 | `getConfigGrid` | `grid(N/Br, B*H)` ↔ `grid(N/Br, H, B)` |

> 所有这些开关的本质都是 **SRAM / 寄存器 / IO 三方权衡**（u7-l2 已建立的总框架）：persist 用更多 SRAM 或寄存器换更少全局 IO；swizzle 用零 SRAM 换无 bank 冲突（padding 则反之）；预取/共享与流水线用更复杂的调度换 g2s 与 MMA 的重叠。改任何一个都意味着不同的 constexpr 模板参数，进而不同的 SRAM 占用与寄存器压力。

#### 4.4.2 核心流程

以 swizzle 与 launch grid 为例，一个开关到 kernel 行为的链路：

```text
ENABLE_FFPA_SMEM_SWIZZLE_Q=1
   ▼ env_cuda_cflags()  ─► -DENABLE_FFPA_SMEM_SWIZZLE_Q
   ▼ nvcc 编译 _C
launch_templates.cuh::getConfigPadQ()
   #ifdef ENABLE_FFPA_SMEM_SWIZZLE_Q  ─► kPadQ = 0   (swizzle 布局)
   ▼
launch_ffpa_attn_fwd_template<..., kPadQ=0, ...>
   ▼ kernel 用 XOR swizzle 布局读 Q 的 SMEM，无 bank 冲突、零 SRAM 浪费
```

#### 4.4.3 源码精读

**预取/共享**：`getConfigPrefetchQKV`（[launch_templates.cuh:115-128](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/launch_templates.cuh#L115-L128)）在开启 `PREFETCH_QKV` 时按 `kStageQKV>1` 决定 `kPrefetchQKV=1`；`getConfigShareSmemQKV`（[launch_templates.cuh:96-103](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/launch_templates.cuh#L96-L103)）把 `QKV_SMEM_SHARE` 译成 `kShareSmemQKV`，后者又参与 SRAM 体积计算（[launch_templates.cuh:221-283](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/launch_templates.cuh#L221-L283) 的 `getConfigQKVSmemMaxSize`）。

**swizzle**：`getConfigPad{Q,K,V}`（[launch_templates.cuh:172-197](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/launch_templates.cuh#L172-L197)）三兄弟，swizzle 开时 `kPad=0`、关时 `kPad=8`。注意 swizzle 与 padding 是「二选一」消 bank 冲突策略（u7-l2 已述）：默认全 swizzle，因 padding 多吃约 50% SRAM。

**persist**：`getConfigPersistQg2s`（[launch_templates.cuh:130-141](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/launch_templates.cuh#L130-L141)）按 `kHeadDim` 与 `kStageQK` 决定 Q 是否常驻 SMEM；`getConfigPersistQs2r`（[launch_templates.cuh:143-152](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/launch_templates.cuh#L143-L152)）控制 Q 常驻寄存器；`getConfigPersistVs2r`（[launch_templates.cuh:154-161](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/launch_templates.cuh#L154-L161)）控制 V 常驻寄存器。`PERSIST_KV_G2S` 影响最广，在 SRAM 体积计算（[launch_templates.cuh:222](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/launch_templates.cuh#L222)）与 tile 选择（[launch_templates.cuh:68](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/launch_templates.cuh#L68)、[launch_templates.cuh:78](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/launch_templates.cuh#L78)）多处出现，并在 D≤256 时切到 FlashAttention 风格的 attention-level tiling。

**流水线/网格**：`getConfigRegistersPipeKV`（[launch_templates.cuh:163-170](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/launch_templates.cuh#L163-L170)）译成 `kRegPipeKV`；`getConfigGrid`（[launch_templates.cuh:205-215](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/launch_templates.cuh#L205-L215)）是 `LAUNCH_GRID_DNHB` 的消费点：

```cpp
template <const int Br>
static inline dim3 getConfigGrid(const int B, const int H, const int N) {
#ifdef ENABLE_FFPA_LAUNCH_GRID_DNHB
  dim3 grid(utils::div_ceil(N, Br), H, B);   // 三维：N/Br, H, B
#else
  dim3 grid(utils::div_ceil(N, Br), B * H);  // 二维：N/Br, B*H（默认）
#endif
  return grid;
}
```

> ⚠️ **一个真实的实现缺陷（待本地验证/反馈上游）**：`env_cuda_cflags()` 为这个开关生成的宏名拼错了，见 [env.py:305-306](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L305-L306)：
>
> ```python
> if cls.enable_launch_grid_dnhb():
>     extra_env_cflags.append("-DENBALE_FFPA_LAUNCH_GRID_DNHB")   # ← ENBALE，不是 ENABLE
> ```
>
> 而所有 kernel 端检查的都是 `#ifdef ENABLE_FFPA_LAUNCH_GRID_DNHB`（如 [launch_templates.cuh:209](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/launch_templates.cuh#L209) 与 [ffpa_attn_fwd.cuh:119](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_fwd.cuh#L119)）。宏名不匹配意味着即便设 `ENABLE_FFPA_LAUNCH_GRID_DNHB=1`，传给 nvcc 的是 `ENBALE_...`，kernel 端的 `#ifdef ENABLE_...` 永远为假——**该开关当前实际是失效的**，网格恒为 `grid(N/Br, B*H)`。读者可用 `grep -rn "ENBALE" env.py csrc/` 自行确认。这正是「构建期 `-D` 宏」机制脆弱性的活教材：宏名拼错不会编译报错，只会静默失效。

#### 4.4.4 代码实践

实践目标：跟踪一个开关从 env 变量到 kernel constexpr 的完整链路。

操作步骤：

1. 任选 `ENABLE_FFPA_SMEM_SWIZZLE_K`，在 [env.py:70-72](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L70-L72) 看其读取；在 [env.py:292-293](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L292-L293) 看它被译成 `-DENABLE_FFPA_SMEM_SWIZZLE_K`；在 [launch_templates.cuh:181-188](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/launch_templates.cuh#L181-L188) 看它决定 `kPadK`。
2. 用 `FFPA_PTXAS_VERBOSE=1`（[docs/env.md:13](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/docs/env.md#L13)）分别构建 swizzle 开/关两版 `_C`，对比 ptxas 报告里单个 kernel 的 SMEM 用量——关掉 swizzle（改用 padding）应使 SMEM 明显变大。

需要观察的现象：swizzle 关闭后 SRAM 占用上升（padding 多吃约 50%），与 u7-l2 结论一致。

预期结果：SMEM 体积随 `kPad` 由 0 变 8 而上升。环境不具备时记为「待本地验证」，可改为静态阅读 `getConfigQKVSmemMaxSize`（[launch_templates.cuh:221-283](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/launch_templates.cuh#L221-L283)）推导 `kPad` 进入 `K_smem_size` 的乘项。

#### 4.4.5 小练习与答案

**练习 1**：`PERSIST_KV_G2S=1` 时，head_dim=256 与 head_dim=512 的 kernel 形态有何不同？

**答案**：`PERSIST_KV_G2S` 默认开启、对 D≤256 生效（见 docs [docs/env.md:54](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/docs/env.md#L54)）。D=256 时 FFPA 自动用 FlashAttention 的 attention-level tiling（整 D 加载，KV 常驻 SMEM）；D=512 超出该阈值，仍走 FFPA 的 MMA 级 Split-D 精细分块。所以同一个开关在不同 D 下触发不同 kernel 形态。

**练习 2**：若想把网格从 `grid(N/Br, B*H)` 换成 `grid(N/Br, H, B)`，设 `ENABLE_FFPA_LAUNCH_GRID_DNHB=1` 就够了吗？

**答案**：按设计意图是的，但由于 [env.py:306](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L306) 的 `ENBALE` 拼写错误，宏名与 kernel 端 `ENABLE_FFPA_LAUNCH_GRID_DNHB` 不匹配，开关当前失效。要真正切换需先修正该拼写（属源码修改，本讲不做）。

---

### 4.5 组合约束：`env_cuda_cflags` 的构建期 assert 与依赖

#### 4.5.1 概念说明

这些开关并非彼此独立，部分组合在 SRAM/寄存器层面互斥或存在依赖。`env_cuda_cflags()` 在生成 `-D` 宏的同时，用一组 `assert` 把非法组合**在构建期**（即编译 `_C` 时）拦截。这意味着错误配置不会拖到运行期才暴露，而是在 `pip install` / JIT `load()` 阶段直接 AssertionError。

注意：这组 assert 只在走「路径 A」（构建 `_C`）时触发；Triton-only 安装根本不调 `env_cuda_cflags()`，所以即便 persist 组合非法，Triton-only 也不会报错（因为这些开关对 Triton 无意义）。

#### 4.5.2 核心流程

```text
构建 _C 时调用 env_cuda_cflags()
   │
   ├── 逐条 append -D宏
   ▼
   末尾的依赖/互斥 assert 块（env.py:310-326）
   │   if PERSIST_KV_G2S:
   │       assert PERSIST_Q_G2S               # KV 常驻依赖 Q 常驻
   │       if QKV_SMEM_SHARE:
   │           assert PERSIST_Q_S2R           # 共享+KV常驻 需 Q 进寄存器腾 SMEM
   │   else:
   │       assert not (PERSIST_Q_S2R and PERSIST_Q_G2S)   # Q 不能同时驻 SMEM 和寄存器
   │       assert not (QKV_SMEM_SHARE and PERSIST_Q_G2S)
   │       assert not (QKV_SMEM_SHARE and PERSIST_KV_G2S)
   ▼ 任一 assert 失败 ──► 构建中止
```

#### 4.5.3 源码精读

约束逻辑见 [env.py:310-326](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L310-L326)，可归纳为三条直觉：

1. **KV 常驻依赖 Q 常驻**：`PERSIST_KV_G2S` 要 Q 也 `PERSIST_Q_G2S`。
2. **Q 不能同时驻 SMEM 和寄存器**：`PERSIST_Q_G2S` 与 `PERSIST_Q_S2R` 互斥（u7-l2 已述 large-D 下 Q 的 g2s 与 s2r 互斥）。
3. **共享 SMEM 与常驻策略互斥**：`QKV_SMEM_SHARE` 不能与 `PERSIST_Q_G2S` 或 `PERSIST_KV_G2S` 同时开（除非配合 `PERSIST_Q_S2R` 把 Q 移出 SMEM 腾空间）。

对照默认值（`PERSIST_Q_G2S=1, PERSIST_KV_G2S=1, QKV_SMEM_SHARE=0, PERSIST_Q_S2R=0`）：走 `if PERSIST_KV_G2S` 分支，第一条 assert 满足（Q_G2S=1），不进 `QKV_SMEM_SHARE` 内层，全部通过——默认配置是自洽的。

#### 4.5.4 代码实践

实践目标：亲手触发一个构建期 assert，理解 fail-fast 行为。

操作步骤：

1. 设置非法组合 `ENABLE_FFPA_PERSIST_KV_G2S=0 ENABLE_FFPA_PERSIST_Q_G2S=1 ENABLE_FFPA_PERSIST_Q_S2R=1`（Q 同时驻 SMEM 与寄存器）。
2. 触发构建期 cflags 生成：`python3 -c "from env import ENV; ENV.env_cuda_cflags()"`。

需要观察的现象：第 2 步直接抛 `AssertionError: PERSIST_Q_G2S and PERSIST_Q_S2R can not both enabled.`

预期结果：证实这组约束在「生成 `-D` 宏时」即被拦截，无需等 nvcc。注意这是「构建期 assert」，仅当你打算编译 `_C` 才相关。

#### 4.5.5 小练习与答案

**练习 1**：默认配置 `PERSIST_KV_G2S=1, PERSIST_Q_G2S=1, QKV_SMEM_SHARE=0` 会触发任何 assert 吗？

**答案**：不会。进入 `if PERSIST_KV_G2S` 分支，第一条 `assert PERSIST_Q_G2S` 满足；`QKV_SMEM_SHARE=0` 不进内层 assert，全部通过。

**练习 2**：为什么这组 assert 放在 `env_cuda_cflags()` 而不是 `__init__` 或运行期？

**答案**：因为这些开关只对编译 `_C` 有意义，而 `env_cuda_cflags()` 正是「要编 `_C`」时才被调用的入口。Triton-only 安装不编译 `_C`、也不调此函数，把这些开关的约束放在这里既能 fail-fast，又不会污染与 CUDA 无关的 Triton 路径。

---

## 5. 综合实践：把运行期开关分成五类并各举一例

本任务把本讲主要内容串起来，对应本讲规格里的实践任务。

### 5.1 实践目标

对照 [docs/env.md](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/docs/env.md) 的「Runtime kernel-selection environment variables」一节（[docs/env.md:29-61](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/docs/env.md#L29-L61)），把所有「kernel 选择」开关分成五类，每类挑一个开关，画出它「翻转后 kernel 行为如何变化」的链路，并标注它是真运行期还是构建期烘焙。

### 5.2 建议产出表格（示例骨架，请自行补全「行为变化」列）

| 类别 | 选中开关 | 默认 | 翻转后 kernel 行为变化 | 真运行期 or 构建期烘焙 |
|---|---|---|---|---|
| MMA 精度 | `FORCE_PV_F16` | 0 | （待补：fp16f32 入口的 P@V acc 从 f32 降到 f16） | 构建期烘焙（`-D` 宏，仅 fp16+acc=f32 有效） |
| 预取/共享 | `QKV_SMEM_SHARE` | 0 | （待补） | 构建期烘焙 |
| swizzle | `SMEM_SWIZZLE_V` | 1 | （待补） | 构建期烘焙 |
| persist | `PERSIST_Q_S2R` | 0 | （待补） | 构建期烘焙 |
| 流水线/网格 | `LAUNCH_GRID_DNHB` | 0 | （待补：意图是网格变三维，但当前因宏拼写错误失效） | 构建期烘焙（且当前失效） |

### 5.3 操作步骤

1. 读 [docs/env.md:29-61](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/docs/env.md#L29-L61)，把每个开关归入五类之一。
2. 对每类选一个开关，在 `env.py` 找到其读取行与 `-D` 翻译行（`env_cuda_cflags`），在 `launch_templates.cuh` 找到其 `getConfigXXX` 消费点。
3. 用一句话写出「翻转后行为变化」，并明确它需要重编 `_C`（构建期烘焙）；唯独 `acc` / `stages` 是真运行期。
4. 额外发现：用 `grep -rn "ENBALE" env.py csrc/` 找出 `LAUNCH_GRID_DNHB` 的宏拼写错误，记入表格注释。

### 5.4 预期结果

- 你应得出结论：**docs/env.md 标为「runtime」的开关里，真正逐调用可变的只有被 `CUDABackend.acc` / `stages` 携带的两个 int；其余全是构建期 `-D` 烘焙。**
- 你应能指出 `LAUNCH_GRID_DNHB` 当前因拼写错误而失效，是一个可向上游反馈的真实问题。
- 若有 GPU 且编译了 `_C`，可选地用 `python -m ffpa_attn.bench`（见 u8-l5）量化某个开关翻转的 TFLOPS 变化；否则全篇可作「源码阅读型实践」完成。

---

## 6. 本讲小结

- **读取时机**：所有 `ENABLE_FFPA_*` / `FFPA_*` 开关都在 `import env` 时被 `ENV` 类一次性快照；进程内改 `os.environ` 不影响已 import 的 `ENV`。
- **两条消费路径**：`acc` / `stages` 是真运行期 int 参数（逐调用可变、零重编）；其余「kernel 选择」开关走 `env_cuda_cflags()` → `-D` 宏 → `launch_templates.cuh` 的 `#ifdef` `constexpr`，**编进 `_C` 即冻结，改了要重编**。docs/env.md 的「runtime」措辞应理解为「不改变生成的翻译单元集合」，而非「无需重编」。
- **MMA 精度三层**：运行期 `acc`（0=f16/1=f32）做大选 → 构建期 `FORCE_QK_F16` / `FORCE_PV_F16` 对 `fp16f32` 入口做混合精度细化 → bf16 在 Python/C++/codegen 三层一致强制 f32 acc。两个 FORCE 宏「互斥」是推荐语义而非 assert。
- **五类开关**：MMA 精度 / 预取·共享 / swizzle / persist / 流水线·网格，本质都是 SRAM/寄存器/IO 三方权衡，各对应一个 `getConfigXXX()` `constexpr` 函数。
- **组合约束**：`env_cuda_cflags()` 末尾的 assert 在构建期 fail-fast 拦截非法 persist/share 组合；只在编译 `_C` 时相关。
- **真实缺陷**：`ENABLE_FFPA_LAUNCH_GRID_DNHB` 因 [env.py:306](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L306) 的 `ENBALE` 拼写错误而当前失效，是构建期宏机制脆弱性的活教材。
- **适用范围**：这些 kernel 选择开关全部是**手写 CUDA 后端专用**；默认 Triton-only 安装不编译 `_C`，开关对 Triton kernel 无任何作用。

---

## 7. 下一步学习建议

- **横向对比 Triton 后端的对应旋钮**：本讲的 swizzle/persist/TMA 在 Triton 后端有同名但语义不同的实现（如 `enable_tma` / `persist_dkdv`），建议进入 u5-l4（反向高级开关）与 u8-l1（Triton autotune）对照阅读，体会「手写 CUDA 用构建期宏 vs Triton 用运行期 config」的设计差异。
- **进入自动调优**：手写 CUDA 的 kernel 选择靠人工设 env 开关；Triton 后端则靠持久化自动调优自动选 config。建议接着学 u8-l1 ~ u8-l3，理解 `lookup_persistent_config` 如何在运行期按 (head_dim, seqlen, ...) 就近匹配最佳 config。
- **二次开发**：若想新增一个 head_dim 或一个运行期开关，回到 u7-l3（per-head_dim 代码生成）与本讲的 `env_cuda_cflags()` / `getConfigXXX()` 链路，按「env 读取 → `-D` 宏 → `constexpr` 消费」三步接入。
- **基准验证**：学完 u8-l5 后，用 `python -m ffpa_attn.bench` 量化本讲任一开关翻转的 TFLOPS 影响，把「读源码」变成「测数据」。
