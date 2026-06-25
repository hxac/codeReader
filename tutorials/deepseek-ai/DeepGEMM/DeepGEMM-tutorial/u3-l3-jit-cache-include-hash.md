# 编译缓存与头文件哈希

## 1. 本讲目标

在 u3-l1 里我们已经知道 `Compiler::build` 是 JIT 的大脑，会用 `(name, signature, flags, code)` 算一个摘要当缓存目录名，并提到「原子整目录 rename」和「include hash 让头文件改动触发重编译」。但这些机制当时只点了名，没有展开。

本讲就把这三件事拆开讲透。学完后你应该能够：

1. 说清 `get_hex_digest` 如何把任意字符串压成一个 128 位摘要，以及 `(name, signature, flags, code)` 四元组里每一项各自负责捕捉「什么变化」。
2. 画出 `IncludeParser` 递归哈希头文件的完整调用栈，解释它为什么只解析 `<deep_gemm/*>`、如何用 `nullopt` 同时做循环检测与记忆化，以及 include hash 是怎么「渗」进最终缓存键的。
3. 复述一次「编译到临时目录 → `fsync_dir` → 原子 `rename`」的全过程，并解释这套设计为何在多进程、分布式文件系统上不会出现半成品目录或段错误。

本讲是 u3-l1、u3-l2 的「下钻」，不重复它们已建立的 JIT 骨架与代码生成内容，只聚焦缓存正确性与并发安全这两个最容易被忽视、却决定整个 JIT 系统能否在生产环境稳定运行的细节。

## 2. 前置知识

阅读本讲前，请确认你已经理解以下概念（均在 u3-l1 / u3-l2 建立）：

- **宿主（host）与设备（device）**：CPU 侧调度代码 vs GPU 侧 `.cuh` 模板。
- **编译期与运行时**：DeepGEMM 只在运行时编译「被请求到的形状」，安装阶段零重型 CUDA 编译。
- **`generate_impl` / `launch_impl`**：每个内核家族的宿主 `Runtime` 类继承自 `LaunchRuntime`，前者生成 `.cu` 源码，后者组装启动参数。
- **cubin 与 kernel 符号**：编译产物是 `kernel.cubin`，`KernelRuntime` 把它加载成可启动的 `KernelHandle`。

本讲还需要两个朴素的背景概念：

- **哈希摘要（digest）**：把任意长度输入映射成定长短串的函数。好摘要满足「输入改一个字节，输出就面目全非」，且碰撞概率极低。它在这里被用作「指纹」——指纹相同就认定内容相同，可以复用缓存。
- **文件系统的原子 rename**：在 POSIX 文件系统（含大多数分布式文件系统）上，把目录 A 改名为 B 是一个原子操作——要么完全成功、要么完全不变，外部观察者永远不会看到一个「改了一半」的目录。这是本讲并发安全的基石。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [csrc/jit/compiler.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp) | `Compiler` 基类，定义缓存目录、`build`（编译→fsync→rename 主流程）、`fsync_dir`/`put`，以及 NVCC/NVRTC 两个子类。 |
| [csrc/jit/cache.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/cache.hpp) | `KernelRuntimeCache`：进程内（内存层）缓存，`dir_path → KernelRuntime`。 |
| [csrc/jit/include_parser.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/include_parser.hpp) | `IncludeParser`：递归解析 `<deep_gemm/*>` 头文件并计算 include hash。 |
| [csrc/utils/hash.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/utils/hash.hpp) | `fnv1a` 与 `get_hex_digest`：底层哈希算法实现。 |
| [csrc/jit/kernel_runtime.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/kernel_runtime.hpp) | `KernelRuntime::check_validity`（依赖原子 rename 的不变量）与 `LaunchRuntime::generate`（注入 include hash 的地方）。 |
| [csrc/utils/system.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/utils/system.hpp) | `get_uuid`（临时目录唯一名）、`safe_remove_all`（并发安全的删除）。 |

贯穿全讲的两个缓存目录约定：

- 磁盘缓存根目录默认是 `$HOME/.deep_gemm`，可用环境变量 `DG_JIT_CACHE_DIR` 覆盖（见 [compiler.hpp:L49-L51](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp#L49-L51)）。
- 其下分 `tmp/`（编译中的临时目录）与 `cache/kernel.<name>.<digest>/`（成品缓存），每个成品目录里固定有 `kernel.cu`（源码）与 `kernel.cubin`（二进制）。

## 4. 核心概念与源码讲解

### 4.1 缓存键与摘要

#### 4.1.1 概念说明

JIT 编译一个 CUDA kernel 通常要几百毫秒到数秒。如果同一个形状每次调用都重编，训练会被编译拖垮。所以 DeepGEMM 维护了一份**持久化缓存**：只要「输入条件」没变，就直接复用上次编出的 cubin。

这就引出一个核心问题：**用什么当缓存的「键」？** 键必须满足——只要键相同，产物就保证等价；键不同，就一定重编。DeepGEMM 的回答是：把所有可能影响产物字节的因素拼成一个字符串，再对这个字符串取摘要，把摘要当目录名。

这个设计有**两层缓存**：

1. **磁盘层**（`$HOME/.deep_gemm/cache/...`）：跨进程持久化，进程重启后依然命中。
2. **内存层**（`KernelRuntimeCache`，进程内 `unordered_map`）：避免同一进程内重复加载同一个 cubin。

`build` 每次先查内存层，再查磁盘层，两层都 miss 才真正编译。

#### 4.1.2 核心流程

缓存键的构造在 `Compiler::build` 开头两行，可以用伪代码概括：

```
kernel_signature = name + "$$" + signature + "$$" + flags + "$$" + code
dir_path = cache_dir / "cache" / ("kernel." + name + "." + hex_digest(kernel_signature))
```

四元组各司其职：

- **`name`**：内核家族名，如 `sm90_fp8_gemm_1d1d`，区分不同的 kernel。
- **`signature`**：编译器身份与版本，如 `NVCC12.9` 或 `NVRTC12.9`。换编译器或升级 CUDA 版本会改变它，从而强制全量重编——这是为了避免「用旧编译器编的 cubin 被新环境误用」。
- **`flags`**：所有编译开关（C++ 标准、`--register-usage-level=10`、`--gpu-architecture=sm_*`、debug/lineinfo 等）。打开 `DG_JIT_DEBUG` 会改 flags，于是重编。
- **`code`**：生成的 `.cu` 源码全文。注意它**已经包含了 include hash 注释**（见 4.2），所以头文件改动也会经此渗入键中；形状/模板参数变化则直接改 code。

字段之间用 `$$` 分隔，防止「字段拼接产生歧义」（否则 `name="a",rest="b"` 与 `name="ab",rest=""` 会拼成同样的串）。

最终的 `hex_digest` 是一个 128 位（32 个十六进制字符）指纹。用生日悖算碰撞概率：\(N \) 个内核发生至少一次碰撞的概率约为

\[
P_{\text{collision}} \approx \frac{N^{2}}{2 \cdot 2^{128}}.
\]

哪怕缓存一百万个内核（\(N=10^{6} \)），这个概率也只有约 \(10^{-27} \)，实际可视为零碰撞。

#### 4.1.3 源码精读

先看摘要算法本身。`get_hex_digest` 用两个不同种子的 FNV-1a 并行计算，各做一个 splitmix64 终结化，再拼成 128 位：

[csrc/utils/hash.hpp:L17-L37](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/utils/hash.hpp#L17-L37) —— 对输入做双状态 FNV-1a 再 splitmix64，输出 32 字符十六进制摘要。

FNV-1a 是一个极轻量的逐字节哈希，递推关系为

\[
h_{0} = \text{seed}, \qquad h_{i} = \bigl((h_{i-1} \oplus c_{i}) \times p\bigr) \bmod 2^{64},
\]

其中 \(p = \texttt{0x100000001b3} \) 是 FNV-1a 的 64 位素数常量（见 [hash.hpp:L7-L15](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/utils/hash.hpp#L7-L15) 的 `fnv1a`）。两个种子 `0xc6a4a7935bd1e995`（MurmurHash 的混合常量）与 `0x9e3779b97f4a7c15`（黄金比例 \(2^{64}/\varphi \)）互相独立，把单路 64 位扩成双路 128 位，碰撞空间从 \(2^{64} \) 提升到 \(2^{128} \)。

再看 `build` 如何用摘要命名目录、并先查两层缓存：

[csrc/jit/compiler.hpp:L100-L107](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp#L100-L107) —— 拼出 `kernel_signature` 与 `dir_path`，先走内存缓存 `kernel_runtime_cache->get(dir_path)`。

内存层 `KernelRuntimeCache::get` 的两级查找逻辑：

[csrc/jit/cache.hpp:L18-L26](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/cache.hpp#L18-L26) —— 先查 `unordered_map` 命中则直接返回；否则用 `KernelRuntime::check_validity` 查磁盘目录是否存在且完整，完整则构造 `KernelRuntime`（加载 cubin）并写入内存缓存；都不命中返回 `nullptr`。

于是 `build` 的前半段就构成「内存 miss → 磁盘 miss → 返回 nullptr → 进入编译分支」的漏斗。编译完成后会再调一次 `get`（见 4.3.3），那时目录已存在，便从磁盘加载并缓存进内存。

#### 4.1.4 代码实践（源码阅读型）

本实践的依据来自 [compiler.hpp:L100-L102](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp#L100-L102) 与 [cache.hpp:L18-L26](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/cache.hpp#L18-L26)。

1. **实践目标**：理解「哪些变化会改键、哪些不会」。
2. **操作步骤**：
   - 阅读上面两段源码，列出会进入 `kernel_signature` 的四个字段。
   - 对下列每一种改动，判断它会不会改变摘要、进而触发重编：
     - (a) 把同一个 kernel 的 `BLOCK_M` 从 128 改成 64；
     - (b) 升级 NVCC 从 12.6 到 12.9；
     - (c) 打开环境变量 `DG_JIT_DEBUG=1`；
     - (d) 修改 `deep_gemm/include` 下某头文件里的一行注释；
     - (e) 只改变输入张量的 M 维大小（运行时形状），不改编译期特化维度。
3. **预期结果**：
   - (a)(b)(c) 直接改 `code` / `signature` / `flags` → 摘要变 → 重编。
   - (d) 通过 include hash 间接改 `code` → 摘要变 → 重编（详见 4.2）。
   - (e) 若该维度未被 `compiled_dims` 特化，则 `code` 不变、摘要不变 → **命中缓存不重编**；这正是 JIT 缓存的收益所在。
4. 该实践为纯源码推导，无需 GPU；若要实测，可在 `DG_JIT_CACHE_DIR` 指向的目录里观察是否新增 `kernel.*` 子目录（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么四元组之间要用 `$$` 分隔，而不能直接拼接？
**答案**：直接拼接会让不同字段组合产生相同字符串。例如 `name="a", signature="b"` 与 `name="ab", signature=""` 拼接后都是 `"ab"`，会造成错误命中。`$$` 作为不出现的分隔符消除了这种歧义。

**练习 2**：内存层缓存（`KernelRuntimeCache`）和磁盘层缓存各解决什么问题？为什么需要两层？
**答案**：磁盘层解决**跨进程**复用——进程重启后仍能命中已编好的 cubin，避免重复编译；内存层解决**同进程内**重复加载 cubin 与符号枚举的开销（加载 cubin 要调 `cuobjdump` 解析符号，见 u4-l3）。两层叠加：进程内先查内存（O(1) 哈希），miss 才查磁盘（文件 IO），都 miss 才编译。

---

### 4.2 include 哈希递归

#### 4.2.1 概念说明

4.1 说 `code` 是缓存键的一部分。但生成出来的 `.cu` 源码本身并不包含被 `#include` 的头文件内容——它只有一行行 `#include <deep_gemm/...>`。如果某天你改了一个头文件（哪怕只改注释），生成代码的文本**完全没变**，于是摘要不变，于是命中旧缓存——可旧 cubin 是按旧头文件编的！这就是「头文件改动不触发重编」的隐患。

`IncludeParser` 就是为消除这个隐患而存在的。它在生成代码被送去 `build` 之前，先把代码里 `#include` 的所有 `<deep_gemm/*>` 头文件**递归地**哈希一遍，得到一个 `include_hash`，再把这个 hash 作为一行注释**拼接进 code 头部**。于是头文件的任何改动都会改变 `include_hash`，进而改变 `code`，进而改变摘要，最终触发重编。

关键设计点有三：

1. **只解析 `<deep_gemm/*>`**：第三方头文件（CUTLASS、CUDA 标准库）不在哈希范围内——它们随包/随 CUDA 发布，版本由 `signature`/CUDA 版本间接保证，且数量巨大、解析慢。
2. **递归**：A include B、B include C，则改 C 也要能感知，所以要把整棵 include 树的内容都折进 hash。
3. **`exclude_code` 语义**：在最外层调用时只哈希「头文件内容」，不哈希「生成代码本身」（因为生成代码已经直接在 `build` 的键里了，不必重复计入 include hash）。

#### 4.2.2 核心流程

include hash 的计算是一个「提取 → 递归 → 折叠」的过程：

```
get_hash_value(code, exclude_code=true):
    ss = ""
    for each include i in get_includes(code):        # 只收 <deep_gemm/*>
        ss += get_hash_value_by_path(include_path / i) + "$"
    if not exclude_code:
        ss += "#" + hex_digest(code)                 # 把本文件自身内容也折进去
    return hex_digest(ss)

get_hash_value_by_path(path):
    if path in cache:
        if cache[path] == nullopt: 报错「可能有循环 include」
        return cache[path]
    code = read(path)
    cache[path] = nullopt                             # 标记「正在算」（循环检测）
    cache[path] = get_hash_value(code, exclude_code=false)   # 递归 + 记忆化
    return cache[path]
```

两个精妙之处：

- **`nullopt` 双重身份**：`cache[path]` 有三种状态——不存在（没算过）、`nullopt`（正在算，用于检测环）、有值（算完了，记忆化复用）。当递归回到一个 `nullopt` 的节点，说明出现了 A→B→A 的环，立即报错。
- **递归时 `exclude_code=false`**：每个头文件自身的哈希 = 「它 include 的子头文件 hash」+「它自己内容的 hash」。这样整棵树的任何一片叶子改动都会层层向上传播到根 hash。

最后，这个 `include_hash` 在 `LaunchRuntime::generate` 里被拼到 code 顶部（见 4.2.3），随后整段 code（含该注释）进入 `build` 的 `kernel_signature`，完成「头文件改动 → 改 hash → 改 code → 改摘要 → 重编」的闭环。

> **一个重要边界**（实践任务的关键）：`include_hash` 在 `generate` 中是 `static` 变量，**每个内核家族、每个进程只算一次**。这意味着头文件改动的检测发生在**进程边界**上——重新运行（新进程）才会重算 include hash。在一个运行中的进程内改头文件不会被察觉；但这无妨，因为你不会在训练 job 跑着的时候去改头文件。

#### 4.2.3 源码精读

include hash 的注入点在 `LaunchRuntime::generate`：

[csrc/jit/kernel_runtime.hpp:L122-L136](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/kernel_runtime.hpp#L122-L136) —— 调子类 `generate_impl` 得到 code；`static std::string include_hash` 仅在首次为空时计算一次（注释明说「we require that generate_impl's includes never change」）；再把 hash 以 `// Includes' hash value: ...` 注释拼到 code 头部返回。

注意返回的 `code` 之后会被传给 `build(name, code)`，于是这行注释就成了 `kernel_signature` 的一部分——这就是 include hash「渗入」缓存键的物理路径。

递归哈希的核心两个方法：

[csrc/jit/include_parser.hpp:L47-L54](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/include_parser.hpp#L47-L54) —— `get_hash_value`：遍历 `get_includes(code)`，对每个头文件调 `get_hash_value_by_path` 并以 `$` 拼接；`exclude_code=true`（默认，最外层调用）时不把 code 自身折入。

[csrc/jit/include_parser.hpp:L56-L73](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/include_parser.hpp#L56-L73) —— `get_hash_value_by_path`：先查 `cache`，命中 `nullopt` 即报「Circular include may occur」；否则读文件、置 `cache[path]=nullopt`（占位防环）、再以 `get_hash_value(code, false)` 递归计算并写入缓存。

`include` 的提取与过滤规则：

[csrc/jit/include_parser.hpp:L16-L38](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/include_parser.hpp#L16-L38) —— 用正则 `#\s*include\s*[<"][^>"]+[>]"` 匹配；只保留 `<deep_gemm...>` 形式且无多余空格的标准 include；凡是不符合的（如 `"foo.h"` 相对路径、带空格的）一律 `DG_HOST_UNREACHABLE` 报错，强制全仓库用统一的 `<deep_gemm/*>` 尖括号写法。

这条「非标准 include 直接报错」的硬约束很关键：它保证哈希范围**确定且封闭**——不会有哪种 include 写法能悄悄逃出 `IncludeParser` 的视野。

#### 4.2.4 代码实践

本实践直接对应任务要求，依据来自 [include_parser.hpp:L16-L73](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/include_parser.hpp#L16-L73) 与 [kernel_runtime.hpp:L122-L136](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/kernel_runtime.hpp#L122-L136)。

1. **实践目标**：亲眼验证「改头文件注释会触发重编译」，并用 `IncludeParser` 的逻辑解释原因。
2. **操作步骤**：
   - 在装有 SM90/SM100 GPU 的环境里，跑一次某个固定形状的 FP8 GEMM（参考 u1-l4），让缓存目录生成。记下 `$HOME/.deep_gemm/cache/` 下对应的 `kernel.<name>.<digest>/` 目录名（含 digest）。
   - 打开该 kernel 实际 `#include` 的某个 `deep_gemm/include/deep_gemm/*.cuh` 头文件，在文件中部加一行无害注释，例如 `// touch for cache test`，保存。
   - **重启进程**后再跑同一形状（必须是新进程，因为 include_hash 是 `static`、进程内只算一次）。
3. **需要观察的现象**：
   - `cache/` 下出现一个**新的** `kernel.<name>.<新digest>/` 目录，旧目录仍在。这表明发生了重编译。
   - 对比新旧目录里的 `kernel.cu`：两者的 `// Includes' hash value: ...` 注释行不同，其余生成代码（模板参数部分）相同。
4. **用 `IncludeParser` 逻辑解释**：新进程里 `generate` 首次调用 `get_hash_value(code)` → `get_includes` 提取到该头文件 → `get_hash_value_by_path` 读取了**改动后**的文件内容 → 其自身 `hex_digest` 改变 → 向上传播使顶层 `include_hash` 改变 → 注释行改变 → `code` 改变 → `build` 里 `kernel_signature` 改变 → `get_hex_digest` 给出**新 digest** → 新目录 → 重编译。
5. **预期结果**：改注释（不影响任何语义）仍触发重编，这正是 include hash 的设计意图——它对内容变化敏感、不区分「语义变化」与「纯注释变化」。若你的环境无 GPU，可在 4.2.5 的练习里纯靠源码推导得到同样结论（待本地验证实测部分）。

#### 4.2.5 小练习与答案

**练习 1**：假设头文件 A `#include <deep_gemm/B>`，B `#include <deep_gemm/C>`。现在只改 C 的一行注释，A 的 include hash 会变吗？为什么？
**答案**：会变。`get_hash_value_by_path(A)` 递归调用时会算到 B，B 又递归算到 C；C 内容变 → C 的 hash 变 → B 折叠后的 hash 变 → A 折叠后的 hash 变。整棵 include 树的任何叶子改动都会逐层向上传播。

**练习 2**：`get_hash_value` 最外层调用用 `exclude_code=true`，而 `get_hash_value_by_path` 内部调 `get_hash_value(code, false)`。为什么最外层要排除 code 自身？
**答案**：最外层的 `code` 是「生成的 kernel 源码」，它**已经作为独立字段进入了 `build` 的 `kernel_signature`**（见 4.1）。若 include hash 再把它折入一次，纯属冗余。`IncludeParser` 的职责只是捕捉「`#include` 引用的头文件内容」，所以最外层只哈希头文件、排除 code；而头文件自身没有「别的渠道」进入缓存键，所以递归时必须把每个头文件的内容也折进去（`exclude_code=false`）。

**练习 3**：为什么 `cache[path] = nullopt` 这一步不能省？
**答案**：它同时承担两个职责。一是**循环检测**：若递归回到一个值为 `nullopt` 的节点，说明正在计算中又被自己引用，即出现 include 环，立即报错；若省略，环会导致无限递归栈溢出。二是**记忆化标记**：计算完成后该值被覆盖为真实 hash，后续再 include 同一头文件直接复用，避免重复读文件与重复递归。

---

### 4.3 原子重命名与 fsync

#### 4.3.1 概念说明

4.1 解决了「缓存键正确」，4.2 解决了「键能感知头文件变化」。本节解决第三个、也是生产环境最棘手的问题：**多个进程（多 rank、多 worker）同时编译同一个 kernel 时，如何不把缓存目录写坏？**

想象 8 张卡上 8 个进程同时首次请求同一个形状：它们都会发现缓存 miss，于是**同时**编译、同时往同一个目标目录写。如果直接写，会出现：

- 两个进程同时写 `kernel.cubin`，互相覆盖、产生半成品文件；
- 一个进程正读、另一个正删，读到损坏数据；
- 在 NFS / Lustre 等分布式文件系统上，并发操作同一父目录甚至可能让 `std::filesystem::remove_all` **段错误**（这是 DeepGEMM 源码注释里明确警告的真实坑）。

DeepGEMM 的解法是经典的「**先在私有临时目录里编译完整，再原子地整个改名过去**」：

1. 每个进程在自己的唯一临时目录 `tmp/<uuid>/` 里编译，互不干扰。
2. 编译完，先 `fsync` 把数据真正刷到磁盘（分布式文件系统上 `close()` 不保证落盘）。
3. 再用 `rename(tmp_dir, dir_path)` 一次性原子地把整个目录搬过去。`rename` 是原子的——要么成功（我赢了，我的成品上线），要么失败（别人已经先上线了，我丢弃自己的版本）。
4. 失败方用并发安全的 `safe_remove_all` 清理自己的临时目录，然后复用赢家的成品。

这套设计让缓存「**要么完整可见、要么完全不可见**」，永不出现半成品。

#### 4.3.2 核心流程

`build` 的编译分支可以用伪代码概括（接在 4.1 的缓存 miss 之后）：

```
tmp_dir = cache_dir / "tmp" / get_uuid()      # 进程私有、全局唯一
make_dirs(tmp_dir)
compile(code, tmp_dir, tmp_dir/"kernel.cubin") # 在临时目录里编完整
fsync_dir(tmp_dir)                              # 自底向上把文件+目录项刷盘
try:
    rename(tmp_dir, dir_path)                   # 原子搬迁；别人已建则失败
except (rename 失败):
    safe_remove_all(tmp_dir)                    # 并发安全地清理；不用 remove_all
runtime = kernel_runtime_cache->get(dir_path)   # 现在目录必然存在 → 加载 cubin
```

几个要点：

- **`get_uuid()`** 含进程 PID 与随机数（见 [system.hpp:L84-L98](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/utils/system.hpp#L84-L98)），保证多个并发编译的临时目录绝不撞名。
- **`fsync_dir` 自底向上**：先递归 fsync 子目录里的文件，最后 fsync 目录自身。注释点明原因是「确保数据**和目录项**在分布式文件系统的其它节点上可见」——光刷文件内容不够，目录条目（这个文件叫什么、属于哪个目录）也要刷，rename 才能被其它节点稳定看到。
- **`safe_remove_all` 替代 `remove_all`**：源码注释明说 `std::filesystem::remove_all` 在分布式文件系统上、多进程操作同一父目录时会段错误；`safe_remove_all` 用 `skip_permission_denied` 选项、逐步 `increment` 迭代器并吞掉错误码来规避（见 [system.hpp:L100-L126](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/utils/system.hpp#L100-L126)）。

#### 4.3.3 源码精读

`build` 的编译分支主体：

[csrc/jit/compiler.hpp:L108-L143](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp#L108-L143) —— 在唯一临时目录编译出 `kernel.cubin`；`fsync_dir(tmp_dir_path)` 刷盘；`make_dirs` 建父目录后 `std::filesystem::rename` 原子搬迁；若 `error_code` 非零（说明别的 rank 抢先建了 `dir_path`），则 `safe_remove_all` 清理自己的临时目录。

注意末尾 [compiler.hpp:L145-L148](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp#L145-L148)：无论我是赢家（rename 成功）还是输家（rename 失败后复用别人的），都再调一次 `kernel_runtime_cache->get(dir_path)`，此时 `dir_path` 必然存在且完整 → 加载 cubin 进内存缓存并返回。

`fsync_dir` 的自底向上递归实现：

[csrc/jit/compiler.hpp:L78-L88](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp#L78-L88) —— 先递归 fsync 子目录、再 fsync 普通文件，最后 fsync 目录自身；注释强调这是为了让「数据 + 目录项」在分布式文件系统的其它节点上可见。

`put`（写源码/cubin 文件）同样带 fsync：

[csrc/jit/compiler.hpp:L90-L98](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp#L90-L98) —— 写文件后立刻 `fsync_path`；注释点明在分布式文件系统上 `close()` 单独不保证落盘，必须显式 fsync 才能让其它进程（如随后被调用的 `nvcc`）读到完整内容。

这套原子性直接支撑了 `check_validity` 的不变量：

[csrc/jit/kernel_runtime.hpp:L96-L110](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/kernel_runtime.hpp#L96-L110) —— 目录存在则 `kernel.cu` 与 `kernel.cubin` **必须同时存在**，因为目录是经原子 rename 整体上线的，绝不会「只有 cu 没有 cubin」。若真缺文件，判定为损坏，提示用户 `rm -rf` 该目录。

> 这个不变量之所以成立，**完全依赖** 4.3 的原子 rename：若是逐文件写入，就可能出现「cubin 写完、cu 还没写」时被别的进程读到的中间态。原子 rename 让「目录可见」与「内容完整」成为同一瞬间的事件。

#### 4.3.4 代码实践（源码阅读型 + 推理型）

1. **实践目标**：理解多 rank 并发编译时「赢家通吃、输家安全退出」的协议。
2. **操作步骤**：
   - 阅读 [compiler.hpp:L108-L148](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp#L108-L148)，标注出：临时目录创建、编译、fsync、rename、失败清理、最终加载这六个阶段。
   - 推演如下并发场景：rank0 与 rank1 同时首次请求同一形状，两者都进入编译分支、都生成了各自的 `tmp/<uuid0>` 与 `tmp/<uuid1>`。
3. **需要观察/推理的现象**：
   - 假设 rank0 的 rename 先成功。rank1 执行 rename 时会发生什么？
   - rank1 清理 `tmp/<uuid1>` 后，为什么不能直接信任自己刚编出的 cubin，而要走 `kernel_runtime_cache->get(dir_path)`？
4. **预期结果**：
   - rank1 的 `rename(tmp/<uuid1>, dir_path)` 会返回非零 `error_code`（目标已存在），于是进入 `safe_remove_all(tmp/<uuid1>)` 分支丢弃自己的产物。
   - rank1 随后 `get(dir_path)` 会读到 rank0 刚原子搬过去的完整目录，加载 rank0 编出的 cubin。这样两 rank 最终用的是**同一份** cubin，且没有谁读到半成品。
   - 推理依据：rename 的原子性 + `check_validity` 对完整性的保证。
5. 该实践为并发协议推导，无需多卡；可在 4.3.5 的练习中加固理解（待本地验证多 rank 实测部分）。

#### 4.3.5 小练习与答案

**练习 1**：为什么编译要写到 `tmp/<uuid>/` 而不直接写到目标 `dir_path`？
**答案**：直接写目标目录会让其它进程在编译过程中看到一个「不完整的目录」（比如只有 `kernel.cu`、`kernel.cubin` 还没生成），`check_validity` 会把它判为损坏、甚至触发错误。先在私有临时目录里编完整、再原子 rename，保证目标目录「要么不存在、要么一定完整」。

**练习 2**：源码注释警告在分布式文件系统上不要用 `std::filesystem::remove_all`。`safe_remove_all` 做了哪三件事来规避段错误？
**答案**：(1) 用 `directory_options::skip_permission_denied` 打开目录迭代器，避免因权限问题抛异常；(2) 在删除每个条目前**先 `increment` 迭代器**拿到下一个条目，这样即使当前条目的删除操作影响了目录状态，迭代器也已安全推进；(3) 全程用 `std::error_code` 重载吞掉错误、遇错即停，而不是让异常传播导致未定义行为。

**练习 3**：`check_validity` 断言「目录存在 ⇒ `kernel.cu` 与 `kernel.cubin` 必同时存在」。这个断言靠什么保证？如果它被违反，会是什么场景？
**答案**：靠 4.3 的「原子整目录 rename + 先 fsync 后 rename」保证——目录只有在临时目录里编译完整、刷盘后才被原子搬上线，所以不可能出现半成品。若被违反，通常是用户手动往缓存目录里塞了残缺文件，或文件系统损坏；此时源码会打印提示并断言失败，建议 `rm -rf` 该目录重建。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次「**从头追踪一个缓存命中/未命中决策**」的端到端推理。

**场景**：你升级了 CUDA Toolkit（NVCC 从 12.6 升到 12.9），随后重新运行某个之前已经缓存过的 FP8 GEMM 形状。

**任务**：请按以下步骤，用本讲三个模块的知识，推理这次运行会发生什么，并给出每一步的源码依据：

1. **键的变化**（用 4.1）：指出 `kernel_signature` 四元组中哪一项会改变、为什么。（提示：看 [compiler.hpp:L194-L200](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp#L194-L200) 里 `signature` 如何由 NVCC 版本拼成。）
2. **缓存的反应**（用 4.1）：新的 digest 不匹配旧目录，内存层与磁盘层都 miss。确认这一判断对应 [cache.hpp:L18-L26](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/cache.hpp#L18-L26) 与 [compiler.hpp:L100-L107](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp#L100-L107) 的哪几行。
3. **include hash 是否参与**（用 4.2）：这次升级 NVCC 不会改任何 `deep_gemm/include` 头文件，所以 include hash **不变**。请解释：既然 include hash 没变，为什么还是会重编？（答：重编由 `signature` 变化驱动，include hash 只是「额外捕捉头文件变化」的补充通道，二者是「或」的关系——任一变化都会经 `code`/`signature` 改变 digest。）
4. **编译与上线的安全性**（用 4.3）：假设你用多卡同时跑，多个 rank 都 miss。描述「私有临时目录编译 → fsync_dir → 原子 rename → 输家 safe_remove_all → 共用赢家 cubin」的完整协议，并指出 [compiler.hpp:L108-L148](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp#L108-L148) 中每一步对应的行。
5. **产物**：运行结束后，`$HOME/.deep_gemm/cache/` 下会多出一个 `kernel.<name>.<新digest>/` 目录，旧 digest 目录仍保留（不会被删，因为不同 signature 天然对应不同目录）。两份 cubin 并存，互不干扰。

**交付物**：一张表格，三列分别是「阶段 / 发生了什么 / 源码行号依据」。完成后，你就把「缓存键 → include 哈希 → 原子上线」这三件事在一条真实场景里贯通了。

> 若本地有 SM90/SM100 环境，可用 `DG_JIT_DEBUG=1` 运行，观察控制台打印的 `kernel_signature`、临时目录、rename 与加载日志来对照你的推理（待本地验证）。

## 6. 本讲小结

- **缓存键 = `(name, signature, flags, code)` 的摘要**：四个字段分别捕捉「内核家族、编译器版本、编译开关、生成源码」的变化；摘要用双状态 FNV-1a + splitmix64 产出 128 位指纹，碰撞概率可视为零。
- **双层缓存**：内存层 `KernelRuntimeCache`（进程内复用 cubin）+ 磁盘层 `$HOME/.deep_gemm/cache/`（跨进程复用），`build` 先内存后磁盘，都 miss 才编译。
- **include hash 闭环**：`IncludeParser` 递归哈希所有 `<deep_gemm/*>` 头文件，结果以注释拼进 `code`，于是头文件改动 → hash 变 → code 变 → digest 变 → 重编；`nullopt` 同时承担循环检测与记忆化。
- **`exclude_code` 的分工**：最外层只哈希头文件（code 已在键里），递归层把每个头文件自身内容也折入，保证整棵 include 树的叶子改动都能向上传播。
- **原子上线协议**：私有临时目录编译 → `fsync_dir` 自底向上刷盘 → 原子 `rename` 搬迁；输家 `safe_remove_all`（而非会段错误的 `remove_all`）清理后复用赢家产物。
- **`check_validity` 依赖原子性**：「目录存在 ⇒ cu 与 cubin 必同时存在」这一不变量，完全由「先编完整再原子整目录 rename」保证。

## 7. 下一步学习建议

本讲把 JIT 缓存的**正确性与并发安全**讲完了。接下来建议：

- **u3-l4（NVCC 与 NVRTC 编译器对比）**：本讲多次提到 `signature` 与 `flags` 来自 NVCC/NVRTC 两个子类，下一讲会详细对比这两个编译后端的实现差异、PCH 加速与 arch 后缀（`100a`/`100f`）选择，正好补全 `Compiler` 继承体系的另一半。
- **u4-l3（内核加载与启动句柄）**：本讲的 `KernelRuntime` 只说了「加载 cubin」，下一讲会展开 `cuLibraryLoadFromFile`、符号枚举与 `LaunchConfig` 的构造，把「缓存命中后如何真正启动」这一段补完。
- **延伸阅读**：想巩固并发文件操作的直觉，可对照阅读 [system.hpp:L100-L126](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/utils/system.hpp#L100-L126) 的 `safe_remove_all` 与 [compiler.hpp:L78-L98](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp#L78-L98) 的 `fsync_dir`/`put`，体会分布式文件系统上「close 不等于落盘、rename 才原子」这两条工程铁律。
