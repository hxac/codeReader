# JIT 架构总览：宿主运行时 vs 设备内核

## 1. 本讲目标

本讲是「JIT 编译系统」单元的第一篇，承接 u2-l3（C++ 绑定与 API 派发层）的终点——一次 GEMM 调用已经经过形状校验、缩放因子变换、按 `get_arch_major()` 派发，到达了某个宿主 Runtime 函数（例如 `sm90_fp8_gemm_1d1d`）。本讲要回答的核心问题是：

> **从「宿主手里拿到了一份 `GemmConfig`」到「GPU 上真正跑起 tensor core 计算」，中间这一段「运行时编译（JIT）」到底发生了什么？**

学完本讲，你应当能够：

1. 说清楚 DeepGEMM 为什么选择**运行时 JIT**，而不是安装时把所有形状的 kernel 预编译好；
2. 顺着 `compiler->build()` 的源码，描述**签名计算 → 缓存查找 → 编译 → 原子重命名 → 写入运行时缓存**的完整流程；
3. 理解 `KernelRuntime` 如何把磁盘上的 `kernel.cubin` 加载成可启动的 `KernelHandle`，以及 `LaunchRuntime` 基类如何用 `generate / launch` 两个钩子把「代码生成」与「内核启动」串起来。

本讲只做**架构总览**，不深入 NVCC vs NVRTC 的对比（u3-l4）、缓存哈希的递归细节（u3-l3）与模板实例化（u3-l2），这些会在后续讲义展开。

## 2. 前置知识

在进入源码前，先用通俗语言建立三个概念。

**宿主（host）与设备（device）。** 宿主指 CPU 侧的调度代码（`csrc/`，编译进 `_C` 扩展）；设备指 GPU 侧真正做矩阵乘的计算代码（`deep_gemm/include/deep_gemm/` 下的 `.cuh` 模板）。一次 GEMM = 宿主准备好参数和内存布局 → 启动一个设备 kernel → 设备 kernel 在 tensor core 上算完 → 结果写回显存。

**编译期（compile-time）与运行时（runtime）。** 传统 CUDA 库在「安装阶段」就用 `nvcc` 把所有 kernel 编译成 `.cubin`（GPU 机器码）。但 DeepGEMM 的设备 kernel 是**模板**，最优的 tile 尺寸（`BLOCK_M/N/K`、cluster 大小、流水线级数等）要等运行时根据实际矩阵形状才能由启发式确定。于是 DeepGEMM 把编译动作推迟到了运行时：每次遇到一个新形状，才生成一段对应的源码并当场编译。这就是 JIT（Just-In-Time）。

**cubin 与 kernel 符号。** `nvcc`/`nvrtc` 把一段 `.cu` 源码编译成 `kernel.cubin`（GPU 二进制）。一个 cubin 里可能含多个设备函数，DeepGEMM 约定每个 cubin 恰好含**一个**可启动 kernel，宿主加载时只需「找出这唯一的那个」即可（这一点会在 4.3 详述）。

如果你对「`_C` 扩展只有薄薄一层、重型 kernel 全靠 JIT」这套设计哲学还印象不深，建议先回顾 u1-l2（构建安装）与 u2-l3（派发层）。

## 3. 本讲源码地图

本讲涉及的关键文件都在 `csrc/jit/` 目录下，这是 JIT 的「基础设施层」：

| 文件 | 作用 | 本讲是否精读 |
|------|------|--------------|
| `csrc/jit/compiler.hpp` | `Compiler` 基类与 `build()` 主流程，以及 NVCC/NVRTC 两个派生编译器 | ✅ 精读 |
| `csrc/jit/kernel_runtime.hpp` | `KernelRuntime`（加载 cubin）、`LaunchRuntime` 基类（generate/launch 钩子） | ✅ 精读 |
| `csrc/jit/cache.hpp` | 内存级 `KernelRuntimeCache`（dir_path → 已加载 kernel 的映射） | 略读 |
| `csrc/jit/include_parser.hpp` | 递归解析 `<deep_gemm/*>` 头文件并计算 include 哈希（u3-l3 深入） | 提及 |
| `csrc/jit/handle.hpp` | CUDA Driver/Runtime API 的薄封装：加载、启动、TMA | 提及 |
| `csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp` | 一个具体的宿主 Runtime 类，演示 generate→build→launch 如何串起来 | 作为案例 |

一句话定位：`compiler.hpp` 负责「把源码变成磁盘上的 cubin」，`kernel_runtime.hpp` 负责「把磁盘上的 cubin 变成 GPU 上可启动的句柄」，二者通过 `cache.hpp` 的缓存和 `build()` 的原子重命名衔接。

## 4. 核心概念与源码讲解

### 4.1 JIT 设计动机：为什么不预编译所有形状

#### 4.1.1 概念说明

最朴素的高性能库做法是：安装时把所有可能的 kernel 都编译好。但 DeepGEMM 的设备 kernel 是高度参数化的模板，决定一个 kernel 形态的维度很多——仅举几类：

- **问题形状**：`M`、`N`、`K`、`num_groups`（分组 GEMM 的 expert 数）；
- **tile/cluster 配置**：`block_m`、`block_n`、`block_k`、`cluster` 大小、`num_stages`（流水线级数）；
- **类型与布局**：`a_dtype`、`b_dtype`、`cd_dtype`、`gemm_type`（Normal / Grouped / Masked / K-Grouped）、swizzle 模式；
- **线程划分**：`num_tma_threads`、`num_math_threads`、`num_sms`。

把这些维度做笛卡尔积，潜在的组合数量是一个巨大的乘积：

\[
N_{\text{kernels}} \;=\; \prod_{d \in \text{特化维度}} |V_d|
\]

其中 \(V_d\) 是维度 \(d\) 的取值集合。哪怕每个维度只有个位数取值，乘起来也是天文数字；而真正在推理/训练中被用到的形状只是其中极小子集。预编译全部组合既**浪费磁盘与构建时间**，也**无法覆盖运行时才确定的形状**（比如变长的 MoE token 数）。

DeepGEMM 的取舍是：**只编译实际被请求的形状，并把结果缓存到磁盘**。这样：

- 安装阶段只需编译一个薄宿主模块 `_C`（见 u1-l2），**完全不碰 CUDA 重型编译**，安装快、跨机分发简单；
- 第一次遇到某形状时付出一次编译开销，之后命中缓存几乎零开销；
- 形状空间无限大也不再是问题——用不到的形状永远不会被编译。

这也是为什么 u1-l2 强调：CUDA Toolkit 既是构建依赖又是运行依赖——因为真正的编译发生在你运行 Python 调用的那一刻。

#### 4.1.2 核心流程

JIT 的整体生命周期可以画成下面这条主线（本讲只画到「加载」，启动细节留给 u4-l3）：

```
   Python 调用 (e.g. fp8_gemm_nt)
            │
            ▼
   派发层 apis/gemm.hpp  (校验 + SF 变换 + arch_major 派发)   ← u2-l3 已讲
            │
            ▼
   宿主 Runtime 函数 (e.g. sm90_fp8_gemm_1d1d)
       │  1. 构造 GemmDesc，调 get_best_config 得到 GemmConfig     ← u5 详讲
       │  2. 构造 TMA 描述符                                        ← u4-l2 详讲
       │  3. code = Runtime::generate(args)        ← 生成 .cu 源码（本讲 + u3-l2）
       │  4. runtime = compiler->build(name, code) ← JIT 编译+缓存（本讲核心）
       │  5. Runtime::launch(runtime, args)        ← 组装 LaunchConfig 并启动
            ▼
        GPU tensor core 执行
```

本讲聚焦在第 3、4、5 步里的「JIT 桥梁」：`generate` 生成源码、`build` 编译并缓存、`KernelRuntime` 加载。

#### 4.1.3 源码精读

先看一个具体宿主 Runtime 函数 `sm90_fp8_gemm_1d1d` 的收尾三行，它把整个 JIT 桥梁串起来：

[csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp:140-143](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp#L140-L143) —— 用 `generate` 生成源码、`compiler->build` 编译缓存、`launch` 启动，三步一线。

```cpp
const auto code = SM90FP8Gemm1D1DRuntime::generate(args);
const auto runtime = compiler->build("sm90_fp8_gemm_1d1d", code);
SM90FP8Gemm1D1DRuntime::launch(runtime, args);
```

这里的 `compiler` 是一个全局懒初始化对象。在 `compiler.hpp` 末尾，它根据环境变量 `DG_JIT_USE_NVRTC` 选择 NVCC 或 NVRTC 后端：

[csrc/jit/compiler.hpp:354-360](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp#L354-L360) —— 全局 `compiler` 用 `LazyInit` 包装，首次解引用时才真正构造，默认走 NVCC，`DG_JIT_USE_NVRTC=1` 时走 NVRTC。

```cpp
static auto compiler = LazyInit<Compiler>([]() -> std::shared_ptr<Compiler> {
    if (get_env<int>("DG_JIT_USE_NVRTC", 0)) {
        return std::make_shared<NVRTCCompiler>();
    } else {
        return std::make_shared<NVCCCompiler>();
    }
});
```

`LazyInit` 的实现很轻：首次 `operator->` 时调用工厂函数构造对象，之后直接复用（见 [csrc/utils/lazy_init.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/utils/lazy_init.hpp)）。这意味着编译器对象（连同它解析出的 NVCC/NVRTC 版本号、组装好的 flags）在整个进程生命周期内只构造一次。

#### 4.1.4 代码实践

**实践目标：** 亲眼看到「不预编译、运行时才编译」这一事实。

**操作步骤：**

1. 找到设备 kernel 模板的位置：`deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh`。注意它是一个 `.cuh` **模板头文件**，仓库里**没有任何预编译好的 `sm90_fp8_gemm_1d1d.cubin`**。
2. 在仓库内搜索 `*.cubin`：

```bash
find . -name '*.cubin' 2>/dev/null
```

**需要观察的现象：** 搜索结果应为空（或最多只有你本地缓存目录里的产物）。这说明发行版**不携带任何预编译 kernel**。

3. 再搜 `__instantiate_kernel`，确认模板实例化只发生在运行时生成的源码里（见 4.3 节的 `generate_impl`）：

```bash
grep -rn "__instantiate_kernel" csrc/ deep_gemm/include/
```

**预期结果：** 命中的是宿主 Runtime 类 `generate_impl` 里那行把模板参数填进去的实例化代码，而不是任何静态预编译产物。

> 待本地验证：是否真的一无所获取决于你本地是否跑过 DeepGEMM 并产生了缓存。发行版源码本身不应包含 `.cubin`。

#### 4.1.5 小练习与答案

**练习 1.** 假设有 3 个 tile 维度，取值数分别为 4、6、8，另有两类 dtype 组合 5 种、3 种 `gemm_type`。若要预编译所有组合，共需多少个 kernel？

**答：** \(4 \times 6 \times 8 \times 5 \times 3 = 2880\)。再叠加上 `num_stages`、`cluster`、`num_groups` 等维度，数量会迅速膨胀到不可接受，这正是 JIT 的动机。

**练习 2.** 为什么 DeepGEMM 的设备 kernel 写成 `.cuh` 模板，而不是写死的普通 `.cu`？

**答：** 因为最优 tile/cluster/流水线配置依赖运行时形状，只能等 `get_best_config` 算出 `GemmConfig` 后，把 `BLOCK_M/N/K` 等当作模板参数填入模板。模板让「同一份算法源码」能特化出无数个具体 kernel，是 JIT 代码生成的前提。

---

### 4.2 build 流程：签名 → 缓存查找 → 编译 → 原子重命名

#### 4.2.1 概念说明

`Compiler::build(name, code)` 是 JIT 的「大脑」，它要解决三个问题：

1. **这次编译的结果该缓存到哪？** 答案是用 `(名字, 编译器签名, flags, 源码)` 算一个哈希摘要当目录名，相同输入必命中同一目录。
2. **是不是已经编译过了？** 先查内存缓存，再查磁盘缓存目录是否存在。
3. **多进程并发安全吗？** 多个 rank（分布式训练常见）可能同时第一次请求同一形状，DeepGEMM 用「编译到临时目录 → 整目录原子 `rename`」避免互相覆盖，并辅以 `fsync` 保证分布式文件系统上的可见性。

这里的关键洞察是：**重命名整个目录是原子的，而逐个文件重命名不是**。注释里明确指出，逐文件重命名会在分布式文件系统上产生 stale inode（陈旧索引节点）问题。

#### 4.2.2 核心流程

`build` 的伪代码如下：

```
build(name, code):
    signature = name + "$$" + compiler.signature + "$$" + flags + "$$" + code
    dir_path  = cache_dir / "cache" / f"kernel.{name}.{hex_digest(signature)}"

    # 第一道：内存缓存
    if runtime_cache.has(dir_path): return runtime_cache[dir_path]

    # 第二道：编译（写进临时目录）
    tmp = cache_dir / "tmp" / uuid()
    compile(code, tmp, tmp/kernel.cubin)        # 由 NVCC/NVRTC 完成
    fsync_dir(tmp)                              # 落盘，保证分布式可见

    # 第三道：原子整目录重命名
    try: rename(tmp, dir_path)
    except:                       # 另一个 rank 抢先了
        safe_remove_all(tmp)      # 丢弃自己的，复用已存在的

    # 第四道：从（现在一定存在的）目录加载并放入内存缓存
    runtime = runtime_cache.get(dir_path)        # 触发 KernelRuntime 构造
    return runtime
```

注意第四道的微妙之处：无论是我编译的、还是别人抢先编译的，`dir_path` 此刻一定存在且完整，于是 `runtime_cache.get(dir_path)` 会构造 `KernelRuntime` 加载 cubin 并返回——这正好衔接到 4.3 节。

哈希用的是 FNV-1a（见 [csrc/utils/hash.hpp:6-42](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/utils/hash.hpp#L6-L42)），把两轮不同种子的结果各经 split-mix64 拼成 32 位十六进制串（128 bit）。FNV-1a 的递推式为：

\[
h_{i+1} = (h_i \oplus c_i) \times p \pmod{2^{64}}, \quad p = \mathtt{0x100000001b3}
\]

其中 \(c_i\) 是输入的字节、初值 \(h_0\) 为给定种子。

#### 4.2.3 源码精读

`build` 的真实代码在 [csrc/jit/compiler.hpp:100-149](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp#L100-L149)。逐段看：

**缓存键与目录名**（L101-L102）：

[csrc/jit/compiler.hpp:101-102](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp#L101-L102) —— 把名字、编译器签名、flags、源码拼成一个字符串再算摘要，得到唯一目录名。

```cpp
const auto kernel_signature = fmt::format("{}$${}$${}$${}", name, signature, flags, code);
const auto dir_path = cache_dir_path / "cache" / fmt::format("kernel.{}.{}", name, get_hex_digest(kernel_signature));
```

`signature` 标识编译器身份（NVCC 时形如 `NVCC12.9`，NVRTC 时形如 `NVRTC12.9`，见 [csrc/jit/compiler.hpp:200](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp#L200) 与 [L260](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp#L260)），所以**换了 NVCC 版本会自动重编译**。`code` 里还含有 include 哈希（4.3 节），所以**改了头文件也会重编译**。

**缓存目录根**（L49-L51）：

[csrc/jit/compiler.hpp:49-51](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp#L49-L51) —— 默认 `$HOME/.deep_gemm`，可用 `DG_JIT_CACHE_DIR` 覆盖。

```cpp
cache_dir_path = std::filesystem::path(get_env<std::string>("HOME")) / ".deep_gemm";
if (const auto env_cache_dir_path = get_env<std::string>("DG_JIT_CACHE_DIR"); not env_cache_dir_path.empty())
    cache_dir_path = env_cache_dir_path;
```

**内存缓存命中即返回**（L105-L106）：

[csrc/jit/compiler.hpp:105-106](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp#L105-L106) —— 先查 `kernel_runtime_cache`，命中则连磁盘都不碰。这就是「同形状第二次调用零编译开销」的原因。

```cpp
if (const auto runtime = kernel_runtime_cache->get(dir_path); runtime != nullptr)
    return runtime;
```

`kernel_runtime_cache` 定义在 [csrc/jit/cache.hpp:29](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/cache.hpp#L29)，它的 `get` 先查内存 map，未命中则校验磁盘目录有效性后构造 `KernelRuntime`（[csrc/jit/cache.hpp:18-26](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/cache.hpp#L18-L26)）。

**编译到临时目录**（L111-L127）：临时目录名用 `get_uuid()` 保证唯一，避免多进程撞车；`DG_JIT_DUMP_ASM/PTX` 时额外产出 PTX 文件。

**`fsync_dir` 后整目录原子 rename**（L130-L143）：

[csrc/jit/compiler.hpp:130-143](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp#L130-L143) —— 先 `fsync` 临时目录，再 `rename` 到最终路径；若 rename 失败说明别的 rank 抢先了，则安全清理自己的副本。

```cpp
fsync_dir(tmp_dir_path);
make_dirs(dir_path.parent_path());
std::error_code error_code;
std::filesystem::rename(tmp_dir_path, dir_path, error_code);
if (error_code) {
    // 另一个 rank 抢先创建，清理自己的、复用已有的
    safe_remove_all(tmp_dir_path);   // 注释强调不能用 remove_all，分布式 FS 上会 segfault
}
```

注释里两个工程要点值得记住：①「重命名目录在本地和分布式文件系统上都是原子的，避免了逐文件重命名出现的 stale inode 问题」；②并发清理时故意不用 `std::filesystem::remove_all`，因为它在分布式文件系统上并发操作同一父目录会段错误，所以改用自写的 `safe_remove_all`。

`fsync_dir`（[csrc/jit/compiler.hpp:80-88](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp#L80-L88)）自底向上先 `fsync` 所有文件和子目录、最后 `fsync` 目录本身——因为目录的 entry（条目）变更也需要单独 `fsync` 才能在其他节点可见。`put`（[L90-L98](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp#L90-L98)）写文件后也会 `fsync`，注释明确：在分布式文件系统上，光 `close()` 不保证落盘。

**放入运行时缓存并返回**（L146-L148）：此刻 `dir_path` 一定存在且完整，`kernel_runtime_cache->get(dir_path)` 会触发 `KernelRuntime` 构造（加载 cubin），这正是 4.3 的入口。

#### 4.2.4 代码实践

**实践目标：** 解读缓存目录命名，并能预测「什么操作会触发重编译」。

**操作步骤：**

1. 假设你已在带 SM90/SM100 GPU 的机器上 `import deep_gemm` 并跑过一次 FP8 GEMM。查看缓存目录：

```bash
ls -la $HOME/.deep_gemm/cache/ | head
```

2. 任选一个 `kernel.sm90_fp8_gemm_1d1d.<一长串十六进制>` 目录，查看其内容：

```bash
ls $HOME/.deep_gemm/cache/kernel.sm90_fp8_gemm_1d1d.<digest>/
# 期望看到: kernel.cu  kernel.cubin  (以及按需的 kernel.ptx / kernel.sass)
```

3. 打开 `kernel.cu` 的第一行，应能看到形如 `// Includes' hash value: <hash>` 的注释（由 4.3 的 `generate` 注入）。

**需要观察的现象与推导：**

- 目录名前缀 `kernel.` + `<name>`（如 `sm90_fp8_gemm_1d1d`）直接来自 `build` 的第一个参数；
- 后缀那串十六进制是 `hex_digest(name$$signature$$flags$$code)`。由此可推断：**改了 tile 配置（code 变）→ digest 变 → 新目录**；**升级了 NVCC（signature 变）→ digest 变 → 全部重编译**；**只改了非模板的运行时参数（如 `num_sms`）但该参数没进模板 → code 不变 → 命中缓存**。

> 待本地验证：实际 digest 串、目录数量取决于你跑过的形状组合。无 GPU 环境下，可改做下方的源码阅读型实践。

**源码阅读型实践（无 GPU 也可做）：** 阅读 [csrc/jit/compiler.hpp:100-149](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp#L100-L149)，回答：如果 `rename` 因目标已存在而失败，当前进程最终拿到的 `runtime` 是自己编译的还是别的 rank 编译的？为什么这里不需要加锁？

**预期结果：** 是「最终存在于 `dir_path` 的那份」，可能是自己的也可能是别人的——但因为整目录是原子重命名，无论哪份都是完整可用的，所以无需锁即可保证正确性。

#### 4.2.5 小练习与答案

**练习 1.** 为什么 `build` 要先 `fsync_dir(tmp)` 再 `rename`，能不能省掉 `fsync`？

**答：** 不能省。在本地文件系统上 rename 后数据基本可见；但在 NFS 等分布式文件系统上，`close()` 和 rename 都不保证数据/目录条目已持久化。若省掉 `fsync`，本 rank rename 成功后，另一个 rank 读到的 `dir_path` 可能是不完整或空的 cubin，导致加载失败。`fsync_dir` 自底向上确保文件内容与目录条目都落盘。

**练习 2.** 两个 rank 同时第一次请求**同一形状**，会发生什么？最终会编译几次？

**答：** 两个 rank 都会执行完整编译（无法避免，因为双方都还没看到对方的目录），各自编译到自己的临时目录。然后双方都尝试 `rename` 到同一 `dir_path`：先成功的一方留下结果，后到的一方 `rename` 失败（`error_code` 非零），清理自己的临时目录，复用已存在的那份。因此磁盘上最终只有一份正确的 cubin，但**编译动作可能执行了两次**——这是为无锁并发付出的代价。

**练习 3.** 若用户设置了 `DG_JIT_CACHE_DIR=/tmp/dg_cache`，缓存会落到哪里？同一个 shape 在两个不同 `DG_JIT_CACHE_DIR` 下是否会重编译？

**答：** 落到 `/tmp/dg_cache/cache/kernel.<name>.<digest>`。会重编译，因为 `build` 只在自己进程的 `cache_dir_path` 下查找；两个不同目录互不可见。这也说明缓存是「按目录隔离」的。

---

### 4.3 KernelRuntime 加载：把 cubin 变成可启动句柄

#### 4.3.1 概念说明

`build` 的产物是一个 `KernelRuntime` 对象。它的职责很纯粹：**把磁盘上的 `kernel.cubin` 加载进 CUDA 运行时/驱动，定位到那唯一的 kernel 符号，对外暴露一个 `KernelHandle`（可启动的函数句柄）**。

为什么要求「一个 cubin 恰好一个 kernel」？因为 JIT 生成的源码（见下方 `generate_impl`）里，唯一被取地址的设备函数就是那个特化好的 `sm90_fp8_gemm_1d1d_impl<...>`。宿主加载时只要「找出这唯一的那个入口」即可，无需按名字匹配——这样生成端不必给 kernel 起稳定名字，加载端逻辑也极简。

`KernelRuntime` 还承担一个隐式契约：**缓存目录只要存在，就一定同时含有 `kernel.cu` 和 `kernel.cubin`**。这是由 4.2 的「原子整目录 rename」保证的——要么整个目录都没出现，要么一出现就是完整的。`check_validity` 正是基于这个契约做完整性校验。

另外，`LaunchRuntime<Derived>` 这个模板基类把「生成源码」和「启动内核」这两个所有 Runtime 类都有的共性抽出来，子类只需实现 `generate_impl`（怎么填模板参数）和 `launch_impl`（怎么传 kernel 参数）两个钩子。这是 DeepGEMM 让几十个 kernel 复用同一套 JIT 脚手架的关键。

#### 4.3.2 核心流程

`KernelRuntime` 构造（加载）流程：

```
KernelRuntime(dir_path):
    cubin_path = dir_path / "kernel.cubin"
    if DG_JIT_DEBUG: print "Loading CUBIN: <cubin_path>"

    if 使用 driver 库枚举 (CUDA >= 12.4 默认):
        kernel = load_kernel(cubin_path, name="")      # 内部用 cuLibraryEnumerateKernels
    else (旧驱动回退):
        symbols = cuobjdump -symbols cubin_path          # 调外部命令列出符号
        过滤出 STT_FUNC + STO_ENTRY、排除 vprintf/__instantiate_kernel 等
        assert 恰好 1 个符号
        kernel = load_kernel(cubin_path, name=该符号)

    # 同时持有 library 句柄，析构时 unload
```

两条路径共享同一个不变量：**最终定位到唯一一个 kernel**。默认的现代路径走 CUDA Driver API 的 `cuLibraryLoadFromFile` + `cuLibraryEnumerateKernels`（进程内、无需外部命令），旧驱动则回退到调用 `cuobjdump -symbols` 解析符号表。

而 `LaunchRuntime` 基类的两个钩子调用方是：

```
generate(args):                              # 静态
    code = Derived::generate_impl(args)       # 子类：fmt::format 把模板参数填进 .cu
    include_hash = (首次) include_parser.get_hash_value(code)   # 递归哈希头文件
    code = "// Includes' hash value: {hash}\n" + code
    return code                               # 这个 code 会进入 build 的签名

launch(runtime, args):                        # 静态
    kernel  = runtime->kernel
    stream  = 当前 CUDA stream
    config  = construct_launch_config(kernel, stream, smem, grid, block, cluster, pdl)
    Derived::launch_impl(kernel, config, args)  # 子类：把张量指针/TMA 描述符等传进去
```

#### 4.3.3 源码精读

**`generate_impl`：把运行时配置固化为编译期常量。** 以 SM90 FP8 GEMM 为例：

[csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp:33-64](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp#L33-L64) —— `fmt::format` 把 `BLOCK_M/N/K`、swizzle、num_stages、线程划分、cluster、num_sms、gemm_type、cd_dtype 等填进模板，生成一段 `#include <deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh>` 后对 `sm90_fp8_gemm_1d1d_impl<...>` 取地址的 `.cu` 源码。

```cpp
static std::string generate_impl(const Args& args) {
    return fmt::format(R"(
#include <deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh>
using namespace deep_gemm;
static void __instantiate_kernel() {{
    auto ptr = reinterpret_cast<void*>(&sm90_fp8_gemm_1d1d_impl<
        {}, {}, {},     // m, n, k
        {},             // num_groups
        {}, {}, {},     // block_m, block_n, block_k
        ... >);
}};
)", get_compiled_dim(args.gemm_desc.m, 'm', ...), ...);
}
```

这段生成的源码里，唯一被取地址的设备函数就是那个特化的 `sm90_fp8_gemm_1d1d_impl<...>`，所以编译出的 cubin 里只有**一个**可启动 kernel——这正是 4.3.1 所说的契约来源。（模板实例化与 `compiled_dims` 的细节留待 u3-l2。）

**`generate`：注入 include 哈希。**

[csrc/jit/kernel_runtime.hpp:122-136](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/kernel_runtime.hpp#L122-L136) —— 调子类 `generate_impl` 得到源码，**首次**用 `include_parser` 递归计算所有 `<deep_gemm/*>` 头文件的哈希，再把它作为注释拼到源码最前面。

```cpp
template <typename Args>
static std::string generate(const Args& args) {
    auto code = Derived::generate_impl(args);
    // NOTES: we require that `generate_impl`'s includes never change
    static std::string include_hash;
    if (include_hash.empty())
        include_hash = include_parser->get_hash_value(code);
    code = fmt::format("// Includes' hash value: {}\n{}", include_hash, code);
    ...
    return code;
}
```

要点：`include_hash` 是 `static`，只算一次，且只哈希**头文件内容**（`exclude_code=true`，见 [csrc/jit/include_parser.hpp:47-54](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/include_parser.hpp#L47-L54)），不哈希 `code` 本身。因为这段哈希随后会被拼进 `code` 再进入 `build` 的签名（4.2），所以**任何被 include 的头文件一改，include_hash 变 → code 变 → digest 变 → 缓存失效重编译**。注释「`generate_impl`'s includes never change」是说每个 Runtime 类 include 的头文件集合是固定的，因此 `static` 缓存哈希是安全的。（递归解析与环检测的完整细节在 u3-l3。）

**`KernelRuntime` 构造：加载 cubin 并定位唯一符号。**

[csrc/jit/kernel_runtime.hpp:35-90](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/kernel_runtime.hpp#L35-L90) —— 加载 `kernel.cubin`，按编译条件走「库枚举」或「cuobjdump 符号解析」两条路径，最终拿到唯一的 `KernelHandle`。

关键片段（默认的现代库枚举路径，L50-L53）：

```cpp
#ifdef DG_JIT_USE_LIBRARY_ENUM_KERNELS
        // Load from the library
        kernel = load_kernel(cubin_path, {}, &library);
#else
        // Find the only symbol —— 调 cuobjdump -symbols 列出符号并过滤
        ...
        DG_HOST_ASSERT(symbol_names.size() == 1);   // 断言恰好一个
        kernel = load_kernel(cubin_path, symbol_names[0], &library);
#endif
```

- 库枚举路径（默认，CUDA Driver ≥ 12.4）：`load_kernel` 内部用 `cuLibraryLoadFromFile` + `cuLibraryGetKernelCount` + `cuLibraryEnumerateKernels`，并断言 kernel 数恰为 1（见 [csrc/jit/handle.hpp:140-154](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/handle.hpp#L140-L154)）。
- 回退路径（旧驱动）：调外部命令 `cuobjdump -symbols`，过滤出 `STT_FUNC`+`STO_ENTRY` 且排除 `vprintf`、`__instantiate_kernel`、`__internal`、`__assertfail`，断言剩下恰好一个（[kernel_runtime.hpp:56-79](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/kernel_runtime.hpp#L56-L79)）。注意 `__instantiate_kernel` 这个宿主函数本身不是设备 kernel，必须排除。

`DG_JIT_DEBUG` 下会打印 `Loading CUBIN: <path>`（[L42-L43](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/kernel_runtime.hpp#L42-L43)）和加载耗时（[L86-L89](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/kernel_runtime.hpp#L86-L89)）——这正是本讲实践要观察的输出。

**`check_validity`：完整性契约。**

[csrc/jit/kernel_runtime.hpp:96-110](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/kernel_runtime.hpp#L96-L110) —— 目录存在时，`kernel.cu` 与 `kernel.cubin` 必须同时存在；否则判定为损坏缓存，提示用户 `rm -rf` 后重启。

```cpp
if (not std::filesystem::exists(dir_path / "kernel.cu") or
    not std::filesystem::exists(dir_path / "kernel.cubin")) {
    printf("Corrupted JIT cache directory ... please run `rm -rf %s` and restart.\n", ...);
    DG_HOST_ASSERT(false and "Corrupted JIT cache directory");
}
```

为什么敢这么断言？因为 4.2 的原子 rename 保证了目录要么不存在、要么完整——绝不会出现「只有 `.cu` 没有 `.cubin`」的中间态。若真出现，说明文件系统被外部破坏，清理重来即可。

**`launch`：组装启动配置并交给子类。**

[csrc/jit/kernel_runtime.hpp:138-162](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/kernel_runtime.hpp#L138-L162) —— 取出 kernel 句柄与当前 stream，用 `construct_launch_config` 组装 grid/block/smem/cluster/PDL，最后调子类 `launch_impl`。

```cpp
template <typename Args>
static void launch(const std::shared_ptr<KernelRuntime>& kernel_runtime, const Args& args) {
    const auto kernel = kernel_runtime->kernel;
    const auto stream = at::cuda::getCurrentCUDAStream();
    LaunchArgs launch_args = args.launch_args;
    launch_args.enable_pdl = device_runtime->get_pdl();        // 允许 Python 运行时覆盖
    auto config = construct_launch_config(kernel, stream, launch_args.smem_size,
                                          grid_dim, block_dim, launch_args.cluster_dim, launch_args.enable_pdl);
    ...
    Derived::launch_impl(kernel, config, args);                // 子类把指针/TMA 描述符传进去
}
```

`construct_launch_config`（[csrc/jit/handle.hpp:174-213](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/handle.hpp#L174-L213)）会把 `cluster_dim > 1` 时的 cluster 维度和 `enable_pdl` 时的 PDL（Programmatic Dependent Launch）属性设进 `CUlaunchConfig`，最终由 `launch_kernel` → `cuLaunchKernelEx` 真正启动（cluster 与 PDL 的含义见 u4-l1/u4-l3）。

#### 4.3.4 代码实践

**实践目标：** 用 `DG_JIT_DEBUG=1` 观察一次 JIT 的「编译 → 加载 → 启动」全链路日志，并定位缓存目录、解释命名由来。

**操作步骤（需要 SM90/SM100 GPU）：**

1. 写一个最小脚本 `jit_obs.py`（示例代码）：

```python
# 示例代码：仅用于触发一次 JIT 编译并观察日志
import torch, deep_gemm
M, N, K = 4096, 4096, 512
a, sfa = deep_gemm.utils.per_token_cast_to_fp8(torch.randn(M, K, device='cuda'))
b, sfb = deep_gemm.utils.per_token_cast_to_fp8(torch.randn(N, K, device='cuda').t().contiguous().t())
d = torch.empty(M, N, device='cuda', dtype=torch.float)
deep_gemm.fp8_gemm_nt((a, sfa), (b, sfb), d)
```

2. 以调试模式运行：

```bash
DG_JIT_DEBUG=1 python jit_obs.py 2>&1 | tee jit.log
```

3. 在 `jit.log` 里依次找以下关键行（顺序大致如下）：
   - `Generated kernel code:` —— 打印生成的 `.cu` 源码（来自 `generate`，[kernel_runtime.hpp:133-134](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/kernel_runtime.hpp#L133-L134)）；其第一行应是 `// Includes' hash value: ...`。
   - `Running NVCC command:` 或 `Compiling JIT runtime with NVRTC options:` —— 实际编译命令（[compiler.hpp:223-224](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp#L223-L224) / [L304-L309](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp#L304-L309)）。
   - `Loading CUBIN: .../kernel.cubin` —— 加载阶段（[kernel_runtime.hpp:42-43](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/kernel_runtime.hpp#L42-L43)）。
   - `Load time (...): X.XX ms` —— 加载耗时（[L86-L89](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/kernel_runtime.hpp#L86-L89)）。
   - `Launch kernel with {...} x ...` —— 启动参数（[L156-L159](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/kernel_runtime.hpp#L156-L159)）。

4. 定位缓存目录并解释命名：

```bash
ls $HOME/.deep_gemm/cache/
# 形如: kernel.sm90_fp8_gemm_1d1d.abcdef0123456789...
```

**需要观察的现象：**
- 第二次运行同一脚本时，`Running NVCC command` 行**消失**（命中内存/磁盘缓存），只剩 `Loading CUBIN` 甚至什么都不打印（内存缓存命中）；
- 目录名 `kernel.<name>.<128bit hex>` 中，`<name>` 就是 `build` 第一个实参（`sm90_fp8_gemm_1d1d`），hex 是 `hex_digest(name$$signature$$flags$$code)`。

**预期结果：** 你能对照源码把日志每一行映射到具体的 `printf` 语句，并解释目录名的两段分别来自哪。

> 待本地验证：无 GPU 环境下无法产生真实日志与缓存目录。此时请改做下方的源码阅读型实践。

**源码阅读型实践（无 GPU）：** 对照本节给出的源码链接，在脑子里「单步执行」一遍 `sm90_fp8_gemm_1d1d` 的收尾三行（[sm90_fp8_gemm_1d1d.hpp:140-143](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp#L140-L143)），列出 `generate → build → KernelRuntime 构造 → launch` 这条链上每一步分别调用了哪个文件的哪个函数，并标注「这一步会打印什么 `DG_JIT_DEBUG` 日志」。

#### 4.3.5 小练习与答案

**练习 1.** 为什么生成的 `.cu` 里用 `reinterpret_cast<void*>(&sm90_fp8_gemm_1d1d_impl<...>)` 对模板实例「取地址」，而不是直接 `__global__ void kernel()` 写一个具名入口？

**答：** 取地址会强制编译器实例化该模板并生成其机器码，但不会引入额外具名 kernel 符号。这样 cubin 里只含这一个被引用的设备函数对应的 kernel，满足「一个 cubin 一个 kernel」的契约，加载端用库枚举即可定位，不必维护具名入口。若写成具名 `__global__` 入口，反而需要处理命名约定。

**练习 2.** 假设你只改动了 `deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh` 里的一行注释，再次运行同一形状，会发生什么？为什么？

**答：** 会重编译。因为 `generate` 注入的 `include_hash` 由 `IncludeParser` 递归哈希所有被 include 的 `<deep_gemm/*>` 头文件的**内容**（连注释也算），注释变化 → include_hash 变 → 拼进 `code` → `build` 的签名变 → digest 变 → 缓存未命中 → 重编译。这正是 include 哈希机制的目的：让头文件改动可靠地触发重编译。

**练习 3.** `KernelRuntime` 析构时为什么要 `unload_library`？如果不卸载会怎样？

**答：** 每个 `KernelRuntime` 持有一个 `LibraryHandle`（`CUlibrary` 或 `cudaLibrary_t`），对应加载进驱动的 cubin 模块。析构时 `unload_library`（[kernel_runtime.hpp:112-114](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/kernel_runtime.hpp#L112-L114)）释放它，避免进程频繁切换大量形状时累积占用驱动侧资源。注意它容忍 `CUDA_ERROR_DEINITIALIZED`（进程退出阶段驱动已关闭），这在 compute-sanitizer 等场景很常见。

---

## 5. 综合实践

**任务：** 把本讲三个最小模块串起来，画一张「JIT 全链路时序图」并配文字解释。

具体要求：

1. **画时序图。** 横轴为时间，纵轴分四列：`宿主 Runtime 函数`、`Compiler::build`、`KernelRuntime / 缓存`、`GPU`。画出从 `sm90_fp8_gemm_1d1d` 收尾三行（[sm90_fp8_gemm_1d1d.hpp:140-143](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp#L140-L143)）开始，到 `cuLaunchKernelEx` 真正启动的完整时序，标注：
   - `generate` 注入 include 哈希的位置；
   - `build` 内部「内存缓存命中 / 未命中→编译→fsync→rename→构造 KernelRuntime」的分支；
   - `KernelRuntime` 加载 cubin、定位唯一符号；
   - `launch` 组装 `LaunchConfig` 并启动。

2. **场景推演。** 针对以下三种场景，分别写出「是否会编译、是否命中缓存、最终目录名是否变化」：
   - (a) 进程内第二次调用同一形状的 GEMM；
   - (b) 新进程第一次调用该形状（磁盘已有缓存）；
   - (c) 升级 NVCC 小版本后第一次调用该形状。

3. **动手验证（可选，需 GPU）。** 用 `DG_JIT_DEBUG=1` 跑 (a)(b)(c)，对照你的推演核对日志与缓存目录。

**参考答案要点：**
- (a) 内存缓存命中（[compiler.hpp:105-106](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp#L105-L106)）：不编译、不加载，直接返回已有 `KernelRuntime`，目录名不变。
- (b) 内存缓存未命中、磁盘缓存命中：`build` 会走到 `kernel_runtime_cache->get(dir_path)` → `check_validity` 通过 → 构造 `KernelRuntime` 加载 cubin（有 `Loading CUBIN` 日志），但**不重新编译**；目录名不变。
- (c) `signature` 从 `NVCC12.x` 变为 `NVCC12.y` → 签名变 → digest 变 → 目录名变化 → 内存与磁盘缓存都不命中 → 重新编译并产生新目录。

## 6. 本讲小结

- DeepGEMM 选择**运行时 JIT** 而非预编译所有形状：设备 kernel 是参数化模板，潜在组合是各特化维度取值数的乘积，天文数字；JIT 只编译被请求的形状并缓存，安装阶段零重型 CUDA 编译。
- `Compiler::build(name, code)` 是 JIT 大脑：用 `(name, signature, flags, code)` 算 FNV-1a 摘要作目录名 → 查内存缓存 → 编译进临时目录 → `fsync_dir` → **整目录原子 rename** → 写入运行时缓存。
- 并发安全靠「原子整目录 rename + fsync」实现，无需加锁；多 rank 抢先时后到者安全清理自己的副本、复用已存在目录（代价是可能重复编译）。
- `KernelRuntime` 把 `kernel.cubin` 加载成 `KernelHandle`，契约是「一个 cubin 恰好一个 kernel」；默认走 Driver API 库枚举（CUDA ≥ 12.4），旧驱动回退到 `cuobjdump -symbols`。
- `LaunchRuntime<Derived>` 基类用 `generate`/`launch` 两个钩子统一所有 Runtime 类：`generate` 调子类 `generate_impl` 生成源码并注入 include 哈希，`launch` 组装 `LaunchConfig` 后调子类 `launch_impl`。
- include 哈希被拼进源码再进入 `build` 签名，使**头文件改动可靠触发重编译**；缓存目录只要存在就必然完整（`kernel.cu` + `kernel.cubin` 齐全），这是原子 rename 的连带保证。

## 7. 下一步学习建议

本讲建立了 JIT 的整体骨架，接下来三篇分别从不同侧面下钻：

- **u3-l2 代码生成与模板实例化**：深入 `generate_impl` 与 `compiled_dims`，看运行时形状如何被固化为编译期模板参数，以及 `get_compiled_dim` 的特化机制。
- **u3-l3 编译缓存与头文件哈希**：展开 `IncludeParser` 的递归哈希与环检测、`get_hex_digest` 的细节，以及分布式文件系统上的原子重命名工程实践。
- **u3-l4 NVCC 与 NVRTC 编译器对比**：对比 `NVCCCompiler`（外部 `nvcc`）与 `NVRTCCompiler`（进程内编译 + PCH）两条后端的实现与取舍。

如果更想先看「启动」一侧，可跳到 **u4-l3 内核加载与启动句柄**，那里会讲 `cuLibraryLoadFromFile`、`construct_launch_config` 里 cluster 与 PDL 属性的设置，以及 `cuLaunchKernelEx` 的最终派发。建议按 u3-l2 → u3-l3 → u3-l4 的顺序读完 JIT 单元，再进入 Unit 4 的启动链路。
