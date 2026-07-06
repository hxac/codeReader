# 编译缓存、原子目录重命名与 cubin 加载

## 1. 本讲目标

本讲承接 u4-l1（JIT 总览）与 u4-l2（内核代码生成），聚焦 JIT 流水线中「编译产物如何被可靠地缓存、共享与加载」这一段。

学完后你应该能够：

- 说清楚 `Compiler::build` 如何用「内核名 + 编译器签名 + 编译标志 + 源码」拼出的字符串算出一个内容寻址（content-addressed）的哈希摘要，并据此定位缓存目录。
- 解释为什么 DeepEP 要「先编译到带 UUID 的临时目录，再把整个目录原子 rename 到最终路径」，以及为什么还要在 rename 前后做 `fsync`。
- 描述多 rank 并发编译同一个内核时， DeepEP 如何用「rename 失败即放弃、改用对端产物」的策略优雅地处理竞态，且避免在分布式文件系统上触发段错误。
- 读懂 `KernelRuntime` 如何用 `cuobjdump -symbols` 从 cubin 里提取「唯一的内核符号」，再做损坏检测，最后用 CUDA Driver API 把 cubin 加载成可启动的 `CUfunction`。
- 能够独立设置 `EP_JIT_CACHE_DIR`，跑一次测试后看懂磁盘上 `cache/kernel.*` 目录的结构，并解释 `kernel.cu` 为何要随 `kernel.cubin` 一起保留。

## 2. 前置知识

在进入本讲之前，请确认你理解以下几个概念（它们在 u4-l1、u4-l2 已建立）：

- **运行时 JIT（Just-In-Time）编译**：DeepEP 不在 `pip install` 时把所有 CUDA 内核编进 `.so`，而是把 `deep_ep/include/impls/*.cuh` 当作 header-only 模板，等运行时知道真实参数（SM 数、rank 数、hidden 等）后，再生成一小段 `.cu`、调用 `nvcc` 实例化出特定的内核。本讲只关心「`.cu` 源码生成之后」的那段：编译、缓存、加载。
- **`generate` / `launch` 两段式**：派生类（如 `DispatchRuntime`）实现 `generate_impl` 产出源码字符串，基类 `LaunchRuntime<Derived>` 负责把它交给 `Compiler::build` 编译，再在 `launch` 里启动。本讲的入口就是 `build`。
- **rank 与多进程**：分布式训练时每个 GPU 对应一个进程（rank），它们往往共享同一个文件系统（可能是本地盘，也可能是 NFS / Lustre 这样的分布式文件系统）。多 rank 会**同时**触发同一个内核的 JIT 编译，这就是并发竞态的根源。
- **cubin**：`nvcc -cubin` 把 `.cu` 编译成的 GPU 二进制（CUDA binary，扩展名 `.cubin`），里面包含可直接由 CUDA Driver API 加载的机器码与内核符号表。
- **`EP_HOST_ASSERT`**：DeepEP 自定义的 host 端断言宏（见 [deep_ep/include/deep_ep/common/exception.cuh:27-28](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/exception.cuh#L27-L28)），条件不成立即打印并中止进程。本讲多处依赖它做损坏检测。

> 阅读提示：本讲引用的行号基于 HEAD `099d5f2`，若你本地代码已更新，请以永久链接为准。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [csrc/jit/compiler.hpp](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp) | `Compiler` / `NVCCCompiler`：缓存目录决策、`build` 主流程、`fsync`、原子 rename | 整个 `build` 函数与文件系统安全细节 |
| [csrc/jit/cache.hpp](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/cache.hpp) | `KernelRuntimeCache`：进程内内存缓存 + 磁盘有效性校验 | 两级缓存的「内存层」 |
| [csrc/jit/kernel_runtime.hpp](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/kernel_runtime.hpp) | `KernelRuntime`：从 cubin 提取符号、加载、损坏检测 | cubin 加载与唯一符号提取 |
| [csrc/utils/hash.hpp](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/utils/hash.hpp) | FNV-1a + split-mix64 哈希，产出 128 位十六进制摘要 | 内容寻址的「指纹」如何算 |
| [csrc/utils/system.hpp](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/utils/system.hpp) | `make_dirs` / `get_uuid` / `safe_remove_all` / `call_external_command` | 临时目录、UUID、安全删除、调用外部命令 |
| [csrc/jit/handle.hpp](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/handle.hpp) | `load_kernel` / `unload_library`：Driver API 加载 cubin | 把符号变成 `CUfunction` |

调用方向回顾（来自 u4-l2 的结论）：

```text
DispatchRuntime::generate_impl  ──►  生成 .cu 源码字符串 code
        │
        ▼
jit::compiler->build("dispatch", code)        ← 本讲入口（dispatch.hpp:228）
        │
        ├─ 算哈希摘要 ──► cache_dir_path/cache/kernel.dispatch.<digest>/
        ├─ KernelRuntimeCache::get(dir)        （内存缓存命中？）
        ├─ 否：编译到 tmp/<uuid>/kernel.cubin
        ├─ fsync_dir(tmp)
        ├─ rename(tmp, dir)   （失败则 safe_remove_all + 复用对端）
        └─ KernelRuntimeCache::get(dir) ──► KernelRuntime(dir)
                                              └─ cuobjdump -symbols → 取唯一符号
                                              └─ cuModuleLoad / cuModuleGetFunction
```

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**编译缓存**（4.1）、**并发竞态与文件系统安全**（4.2）、**cubin 加载**（4.3）。三者共同回答一个问题：同一段 `.cu` 源码第二次被请求时，如何做到「零编译、零竞态、零加载错误」地拿到可启动的内核。

### 4.1 内容寻址的编译缓存

#### 4.1.1 概念说明

「缓存」最朴素的做法是用一个自增 ID 或时间戳当 key。但 DeepEP 的缓存 key 不能这么做——因为它要在**多个 rank、多次运行、甚至不同机器**之间共享同一块磁盘缓存目录。如果 key 不能唯一、稳定地由「内核本身」决定，就会出现：明明源码没变却重复编译（浪费几十秒），或者源码变了却误命中旧产物（用错内核，结果错误）。

DeepEP 选择的是**内容寻址（content-addressed）**缓存：把「凡是会影响 cubin 二进制的因素」全部拼成一个字符串，对它做哈希，用哈希摘要作为磁盘上的目录名。这样一来：

- 同样的输入 ⇒ 同样的摘要 ⇒ 命中同一个目录 ⇒ 直接复用，无需重编译。
- 任何一个因素变化（哪怕改了一个编译标志、升了一档 nvcc 版本） ⇒ 摘要变化 ⇒ 落到新目录 ⇒ 自动重新编译，绝不会误用旧产物。

那么「会影响 cubin 的因素」有哪些？DeepEP 在 [csrc/jit/compiler.hpp:112](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp#L112) 里列了四项，用 `$$` 分隔拼成「内核签名」：

1. **内核名 `name`**（如 `"dispatch"`、`"dispatch_copy_epilogue"`）：区分不同内核。
2. **编译器签名 `signature`**（如 `"NVCC12.3"`）：编译器升级后旧 cubin 失效。
3. **编译标志 `flags`**（含 `--gpu-architecture=sm_90`、`--register-usage-level=10`、各种 `-D` 宏等）：换卡型、换优化等级后失效。
4. **源码 `code`**：参数一变，u4-l2 生成的模板特化就变，源码自然变。

> 注意：源码 `code` 的最开头还插入了「头文件哈希」注释行（见 [csrc/jit/launch_runtime.hpp:42](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/launch_runtime.hpp#L42)），所以被 `#include` 的头文件内容变化也会反映进摘要——这是内容寻址能覆盖「间接依赖」的关键。

#### 4.1.2 核心流程

哈希算法用的是 **FNV-1a**（Fast，分布均匀、实现极简、对短串也敏感），再用 **split-mix64** 做最终混淆，拼成 128 位（32 个十六进制字符）摘要。直觉上：

- FNV-1a 对输入逐字节处理：每读入一个字节 `c`，先与当前哈希异或，再乘一个大质数 `prime`。

\[
h_0 = \text{seed},\qquad h_i = (h_{i-1} \oplus \text{byte}_i) \times p,\quad p = \texttt{0x100000001b3}
\]

- 为了得到 128 位摘要，DeepEP 用**两个不同的 seed** 各跑一遍 FNV-1a，得到 `state_0`、`state_1` 两个 64 位值。
- 再各过一遍 split-mix64 混淆（改善雪崩性质，避免相近输入产生相近哈希）：

\[
\text{splitmix}(z) = \big(\text{mix}_3 \circ \text{mix}_2 \big)(z),\quad
\text{mix}_k(z) = (z \oplus (z \gg s_k)) \times m_k
\]

最终把两个 64 位结果拼成 32 位 hex 串，就是目录名里的 `<digest>`。整个缓存目录形如：

```text
$EP_JIT_CACHE_DIR/                         # 默认 ~/.deep_ep
├── cache/
│   └── kernel.dispatch.<32-hex 摘要>/
│       ├── kernel.cu                       # 生成出的源码（保留）
│       └── kernel.cubin                    # 编译产物
└── tmp/                                    # 临时编译区
    └── <uuid>/                             # 带 pid 的唯一名（见 4.2）
```

#### 4.1.3 源码精读

先看缓存目录的根怎么定（[csrc/jit/compiler.hpp:52-54](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp#L52-L54)）：默认是 `$HOME/.deep_ep`，若设了 `EP_JIT_CACHE_DIR` 则覆盖。这正是本讲实践任务要操作的开关。

接着看签名拼接与目录定位（[csrc/jit/compiler.hpp:111-113](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp#L111-L113)），这段是内容寻址的「定义点」：

```cpp
std::shared_ptr<KernelRuntime> build(const std::string& name, const std::string& code) const {
    const auto kernel_signature = fmt::format("{}$${}$${}$${}", name, signature, flags, code);
    const auto dir_path = cache_dir_path / "cache" / fmt::format("kernel.{}.{}", name, get_hex_digest(kernel_signature));
```

`get_hex_digest` 的实现见 [csrc/utils/hash.hpp:18-34](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/utils/hash.hpp#L18-L34)，正是上面描述的双 seed FNV-1a + split-mix64。注意目录名里**同时**保留了可读的 `name` 和不可读的 `digest`：前者方便人眼定位「这是哪个内核」，后者保证唯一性与正确性。

`NVCCCompiler` 的构造函数（[csrc/jit/compiler.hpp:204-220](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp#L204-L220)）会重写 `signature`（塞入 `nvcc` 版本）与 `flags`（塞入 `-I include` 路径、`--gpu-architecture=sm_<arch>` 等），所以「换卡型 / 换 nvcc」会自然改变摘要。

#### 4.1.4 代码实践

**实践目标**：亲手看到一个内容寻址缓存目录长什么样，并验证「同样输入命中同目录、改输入换目录」。

**操作步骤**（源码阅读 + 本地运行，运行部分需 GPU 环境）：

1. 设置一个干净的缓存目录并打开 JIT 调试日志：
   ```bash
   export EP_JIT_CACHE_DIR=/tmp/deepep_jit_demo
   export EP_JIT_DEBUG=1
   rm -rf /tmp/deepep_jit_demo
   ```
2. 在单机 8 卡上跑一次 dispatch 测试（命令参考 u1-l4，如 `torchrun --nproc-per-node=8 tests/elastic/test_ep.py ...`）。
3. 观察日志里 `Running NVCC command:` 一行，它会打印编译到的临时路径。
4. 跑完后列出缓存：
   ```bash
   ls -la /tmp/deepep_jit_demo/cache/
   ls -la /tmp/deepep_jit_demo/cache/kernel.dispatch.*/
   ```
   你应看到 `kernel.cu` 与 `kernel.cubin` 两个文件。
5. **再跑一次**同样参数的测试，观察日志：这次**不会**再出现 `Running NVCC command:`，因为命中了磁盘缓存。

**需要观察的现象**：
- 第一次：`cache/` 下新增 `kernel.dispatch.<32 位 hex>/`；日志有 nvcc 命令。
- 第二次：`cache/` 下目录名**完全不变**（同样输入 ⇒ 同样摘要）；日志无 nvcc 命令。
- 改一个会影响 cubin 的因素（例如换 `--num-experts` 导致模板参数变化），会看到**新的** `kernel.dispatch.<不同 hex>/` 目录。

**预期结果**：磁盘上每个内核配置对应恰好一个内容寻址目录，重复运行零编译。

> 待本地验证：若无 GPU/多卡环境，可只做步骤 1、4 的源码阅读——对照 [csrc/jit/compiler.hpp:111-113](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp#L111-L113) 理解目录名如何由摘要决定即可。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `EP_JIT_CPP_STANDARD` 从默认 20 改成 17，已经编译好的内核会被复用吗？为什么？

> **答案**：不会。`flags` 里含有 `-std=c++{EP_JIT_CPP_STANDARD}`（[csrc/jit/compiler.hpp:58-60](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp#L58-L60)），改它 ⇒ `flags` 变 ⇒ `kernel_signature` 变 ⇒ 摘要变 ⇒ 落到新目录重新编译。这正是内容寻址「绝不会误用旧产物」的体现。

**练习 2**：为什么目录名要同时保留 `kernel.dispatch` 这个可读前缀和后面的哈希？只用哈希不行吗？

> **答案**：只用哈希在功能上完全可行（唯一性由哈希保证）。保留可读前缀纯粹是为了**可维护性**：当缓存目录堆积了几十个条目时，运维人员能一眼看出哪个目录对应哪个内核，方便排查与清理。哈希负责正确，前缀负责可读。

---

### 4.2 多 rank 并发编译：临时目录 + 原子 rename + fsync

#### 4.2.1 概念说明

内容寻址解决了「正确复用」，但带来一个新问题：**多 rank 同时编译同一个内核时，它们算出的目录路径完全相同**。如果每个 rank 都直接往 `dir_path` 里写文件，就会出现「一个 rank 写到一半，另一个 rank 读到残缺 cubin」的竞态；在 NFS / Lustre 这类分布式文件系统上，并发操作同一个父目录还可能触发段错误。

DeepEP 的解法是经典的「**先写到唯一的临时位置，再原子地一次性到位**」模式：

1. 每个 rank 编译到自己的私有临时目录 `tmp/<uuid>/`，互不干扰。
2. 编译完成后，用 `rename(tmp, dir_path)` **把整个目录一次性改名**。在本地与分布式文件系统上，「目录 rename」都是原子的——要么成功（对端瞬间看到一个完整的目录），要么失败（什么都没发生）。
3. 失败意味着「别的 rank 抢先建好了 `dir_path`」，于是本 rank 丢弃自己的临时目录，直接复用对端的成果。

这一套把「编译」这件慢且非原子的事，变成了「rename」这件快且原子的事，竞态窗口被压缩到几乎为零。

#### 4.2.2 核心流程

```text
build(name, code):
  ① 算 dir_path
  ② KernelRuntimeCache::get(dir_path)  ──内存命中──► 直接返回
  ③ tmp_dir_path = tmp/<uuid>/            （uuid 含 pid，见 system.hpp:get_uuid）
  ④ 在 tmp_dir_path 里：写 kernel.cu、nvcc 编出 kernel.cubin
  ⑤ fsync_dir(tmp_dir_path)               （自底向上把数据与目录项刷盘）
  ⑥ rename(tmp_dir_path, dir_path)
        ├─ 成功 ──► 本 rank 是赢家
        └─ 失败 ──► safe_remove_all(tmp_dir_path)   （对端赢了，清理自己）
  ⑦ KernelRuntimeCache::get(dir_path)  ──► 加载并返回
```

几个关键设计点：

- **为什么 rename 整个目录而不是单个文件**：注释明确说，重命名单个文件会引发「stale inode（陈旧 inode）」问题（[csrc/jit/compiler.hpp:120-121](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp#L120-L121)）；目录 rename 在本地与分布式文件系统上都原子。
- **为什么用 `safe_remove_all` 而非 `std::filesystem::remove_all`**：后者在分布式文件系统上、多进程并发操作同一父目录时会**段错误**（[csrc/jit/compiler.hpp:150-153](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp#L150-L153)）。`safe_remove_all` 用 `skip_permission_denied` + 逐项递增迭代 + `error_code` 重载，避免抛异常与段错误（见 [csrc/utils/system.hpp:83-109](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/utils/system.hpp#L83-L109)）。
- **为什么 rename 前要 fsync**：在分布式文件系统上，`close()` 不保证数据真正落盘并对其它节点可见；若不 fsync 就 rename，对端 rank 可能「看到目录却读到空 cubin」。`fsync_dir` 自底向上先同步所有文件、再同步目录本身（[csrc/jit/compiler.hpp:89-99](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp#L89-L99)）。

#### 4.2.3 源码精读

`build` 的并发安全主体在 [csrc/jit/compiler.hpp:119-159](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp#L119-L159)。摘关键几段：

临时目录用带 pid 的 UUID 保证全局唯一（[csrc/jit/compiler.hpp:122-123](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp#L122-L123)）；UUID 实现见 [csrc/utils/system.hpp:67-81](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/utils/system.hpp#L67-L81)，由 `getpid()` 加三段随机数拼成，所以即便两个 rank 同一毫秒编译，目录名也不会撞。

rename 与失败处理（[csrc/jit/compiler.hpp:145-154](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp#L145-L154)）：

```cpp
make_dirs(dir_path.parent_path());
std::error_code error_code;
std::filesystem::rename(tmp_dir_path, dir_path, error_code);   // 原子
if (error_code) {
    // 另一个 rank 抢先了：清理自己的临时目录，改用已有的
    safe_remove_all(tmp_dir_path);
}
```

注意这里用的是 `rename` 的 `error_code` 重载——失败不抛异常，而是进入「我输了，复用对端」分支。无论输赢，最后都执行 `kernel_runtime_cache->get(dir_path)`（[csrc/jit/compiler.hpp:157-158](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp#L157-L158)）去加载最终目录，赢家加载自己刚 rename 的、输家加载对端早已建好的，二者殊途同归。

写文件 `put` 也会 fsync（[csrc/jit/compiler.hpp:101-109](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp#L101-L109)），保证 `kernel.cu` 写盘后 nvcc（可能在不同进程/节点）能立刻读到完整内容。

#### 4.2.4 代码实践

**实践目标**：理解「目录 rename 是原子的、文件 rename 会留隐患」这一设计取舍。

**操作步骤**（源码阅读型，无需运行）：

1. 阅读 [csrc/jit/compiler.hpp:119-154](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp#L119-L154)，在纸上模拟「rank A 和 rank B 同时编译 `dispatch`」的时间线：两者算出相同 `dir_path`，各自在 `tmp/<uuidA>`、`tmp/<uuidB>` 编译；假设 A 先 rename 成功，B 的 rename 返回 `error_code`，B 走 `safe_remove_all` 分支。
2. 阅读 [csrc/utils/system.hpp:83-109](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/utils/system.hpp#L83-L109) 的 `safe_remove_all`，对比它与 `std::filesystem::remove_all` 的区别：它用 `directory_options::skip_permission_denied`、手动 `increment(ec)`、每步吞掉 `error_code`。

**需要观察的现象**（推理）：如果改成「每个文件单独 rename 进 `dir_path`」，那么在「A 刚把 `kernel.cubin` rename 进去、还没来得及放 `kernel.cu`」的瞬间，B 调用 `check_validity(dir_path)` 会发现「目录存在但缺 `kernel.cu`」，触发损坏断言（见 4.3）。整目录 rename 消除了这个窗口。

**预期结果**：能用自己的话讲清「整目录原子 rename + 失败即复用对端」如何把多 rank 编译竞态压到零窗口。

> 待本地验证：若想实测，可在两台共享 NFS 的机器上同时跑同一内核，用 `ls` 抓拍 `tmp/` 下应出现两个不同 `<uuid>` 目录；该现象需分布式环境。

#### 4.2.5 小练习与答案

**练习 1**：假设把 `std::filesystem::rename(tmp_dir_path, dir_path, error_code)` 改成「先 `create_directories(dir_path)` 再把 `kernel.cubin` 复制过去」，会引入什么问题？

> **答案**：会重新打开竞态窗口。对端可能在「目录已建、cubin 还没复制完」时读到残缺产物；而且分多次文件操作不再是原子的，分布式文件系统上还可能触发 stale inode。整目录 rename 的全部意义就在于把「多步、非原子」压缩成「一步、原子」。

**练习 2**：rename 失败后，DeepEP 为什么不重试，而是直接复用对端目录？

> **答案**：因为失败已经证明「`dir_path` 此刻存在且由对端负责填充」，重试 rename 只会再次失败。复用对端成果既省一次编译，又避免两个 rank 同时写同一目录。最后统一的 `kernel_runtime_cache->get(dir_path)` 会负责校验对端产物是否真的完整（见 4.3 的 `check_validity`）。

---

### 4.3 cubin 加载与唯一符号提取

#### 4.3.1 概念说明

到这一步，磁盘上 `dir_path/kernel.cubin` 已经就位（无论是本 rank 编的，还是对端编好被复用的）。但 cubin 是一个**容器**：一个 cubin 里可能有多个符号——除了真正的内核入口函数，还可能有 `vprintf`、`__assertfail` 这类 runtime 辅助符号，以及 u4-l2 提到的、用来强制模板实例化的壳函数 `__instantiate_kernel`。要启动内核，必须**精确地**从里面挑出「那一个」真正的内核符号，再用 Driver API 把它加载成 `CUfunction`。

DeepEP 的做法是用 `cuobjdump -symbols <cubin>` 列出全部符号，再用一组「黑名单 + 白名单」规则过滤：

- **白名单**：符号行同时满足以 `STT_FUNC`（是一个函数）开头、含 `STO_ENTRY`（是入口符号）。
- **黑名单**：符号名含 `vprintf` / `__instantiate_kernel` / `__internal` / `__assertfail` 的一律剔除——它们不是要启动的内核。

过滤后**必须恰好剩 1 个**符号。若剩 0 个或多个，说明 cubin「损坏」或生成代码有误，DeepEP 会打印明确的修复提示（`rm -rf <dir>` 后重启）并断言失败。这种「严格唯一 + 友好报错」的设计，让缓存损坏时不会被静默吞掉，而是立即、可定位地失败。

#### 4.3.2 核心流程

```text
KernelRuntime(dir_path):
  ① cubin_path = dir_path / "kernel.cubin"
  ② 运行: cuobjdump -symbols kernel.cubin      （拿回全部符号文本）
  ③ 逐行解析：保留 (STT_FUNC 且 STO_ENTRY) 且 不含黑名单词 的符号
  ④ 若 symbol_names.size() != 1 ──► 打印"Corrupted JIT cache directory" + 断言失败
  ⑤ load_kernel(cubin_path, symbol_names[0]):
        ├─ cuModuleLoad(cubin_path)  ──► CUmodule
        └─ cuModuleGetFunction(module, name) ──► CUfunction   ← 可启动的内核
```

损坏检测分两层：一层在「进缓存」之前由 `check_validity` 做（目录与必备文件是否存在），一层在「加载」时由「符号唯一性」做（cubin 内容是否合法）。两层缺一不可。

#### 4.3.3 源码精读

`KernelRuntime` 构造函数即加载流程，见 [csrc/jit/kernel_runtime.hpp:20-59](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/kernel_runtime.hpp#L20-L59)。黑名单定义与符号解析在 [csrc/jit/kernel_runtime.hpp:31-43](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/kernel_runtime.hpp#L31-L43)：

```cpp
const std::vector<std::string> illegal_names = {"vprintf", "__instantiate_kernel", "__internal", "__assertfail"};
const auto [exit_code, symbols] = call_external_command(
    fmt::format("{} -symbols {}", cuobjdump_path.c_str(), cubin_path.c_str()));
// ...逐行解析：保留 STT_FUNC + STO_ENTRY 且不在黑名单的符号...
```

注意 `__instantiate_kernel` 正是 u4-l2 里那个承载 `reinterpret_cast<void*>(&func<...>)` 的壳函数——它在符号提取阶段被特意过滤掉，保证剩下的唯一符号就是真正的 `dispatch_impl` / `combine_impl` 等内核。

损坏检测与友好报错在 [csrc/jit/kernel_runtime.hpp:46-55](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/kernel_runtime.hpp#L46-L55)：当符号数不为 1 时，会打印「expected 1 kernel symbol, found N」并列出全部符号、给出 `rm -rf <dir>` 的修复指令，然后断言失败。

最后一步加载见 [csrc/jit/kernel_runtime.hpp:58](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/kernel_runtime.hpp#L58)，调用 `load_kernel`。默认走 CUDA **Driver API** 分支（[csrc/jit/handle.hpp:82-92](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/handle.hpp#L82-L92)）：`cuModuleLoad` 把 cubin 装载成 `CUmodule`，`cuModuleGetFunction` 取出符号对应的 `CUfunction`，析构时（[csrc/jit/kernel_runtime.hpp:80-82](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/kernel_runtime.hpp#L80-L82)）再 `cuModuleUnload` 卸载。

`check_validity` 在缓存入口把关（[csrc/jit/kernel_runtime.hpp:65-78](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/kernel_runtime.hpp#L65-L78)）：目录必须存在，且 `kernel.cu` 与 `kernel.cubin` **同时**存在——这正是 4.2 里「整目录原子 rename」要保证的不变式（要么一个完整目录、要么没有目录）。两者缺一即判定损坏。

`KernelRuntimeCache::get`（[csrc/jit/cache.hpp:21-29](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/cache.hpp#L21-L29)）是两级缓存的「内存层」：先查进程内 `unordered_map`，命中直接返回；否则调 `check_validity` 校验磁盘、再构造 `KernelRuntime` 加载，并塞回内存缓存。这就是 `build` 开头能「零编译命中」、结尾能「零重复加载」的原因。

#### 4.3.4 代码实践

**实践目标**：手动复现「从 cubin 提取唯一符号」的过程，直观理解黑名单的作用。

**操作步骤**（源码阅读型，无需运行）：

1. 假设你已经通过 4.1 的实践得到了 `/tmp/deepep_jit_demo/cache/kernel.dispatch.<digest>/kernel.cubin`，手动执行 DeepEP 在代码里跑的同一条命令（见 [csrc/jit/kernel_runtime.hpp:32](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/kernel_runtime.hpp#L32)）：
   ```bash
   cuobjdump -symbols /tmp/deepep_jit_demo/cache/kernel.dispatch.*/kernel.cubin
   ```
2. 在输出里找出以 `STT_FUNC` 开头且含 `STO_ENTRY` 的行，剔除含 `vprintf`/`__instantiate_kernel`/`__internal`/`__assertfail` 的行，确认**只剩一行**，其末尾的符号名就是被加载的内核。
3. 对照 [csrc/jit/handle.hpp:82-92](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/handle.hpp#L82-L92) 的 `load_kernel`：把这一符号名传给 `cuModuleGetFunction` 即得到 `CUfunction`。

**需要观察的现象**：原始符号列表里有多个 `STT_FUNC` 行，但经过黑名单过滤后只剩唯一一个真正的内核符号——验证 4.3.1 的过滤规则。

**预期结果**：你能指出「哪一行是被启动的真内核」「哪些行是被过滤掉的辅助/壳符号」。

> 待本地验证：若没有 cubin，可纯阅读：对照 u4-l2 的 `__instantiate_kernel` 壳函数与 [csrc/jit/kernel_runtime.hpp:31](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/kernel_runtime.hpp#L31) 的黑名单，理解为何壳函数必须被排除。

**关于「为什么保留 `kernel.cu`」**（这是本讲规格里要回答的问题）：原因有三。其一，`check_validity`（[csrc/jit/kernel_runtime.hpp:70-71](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/kernel_runtime.hpp#L70-L71)）把「`kernel.cu` 与 `kernel.cubin` 同时存在」当作目录完整性的不变式，缺 `kernel.cu` 即判损坏。其二，调试场景下（`EP_JIT_DUMP_PTX`、`EP_JIT_DUMP_SASS`）需要源码在场才能对照反汇编（见 [csrc/jit/compiler.hpp:127-138](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp#L127-L138)）。其三，内容寻址的精髓是「摘要由源码决定」，保留 `kernel.cu` 让人能随时审计「这个 cubin 到底是由哪段源码编出来的」，是缓存可解释性的基础。

#### 4.3.5 小练习与答案

**练习 1**：如果某次代码生成不慎往 cubin 里塞进了**两个**真正的内核符号（都被白名单通过），`KernelRuntime` 会怎样？

> **答案**：`symbol_names.size()` 变成 2，触发 [csrc/jit/kernel_runtime.hpp:46-55](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/kernel_runtime.hpp#L46-L55) 的「Corrupted JIT cache directory (expected 1 kernel symbol, found 2)」分支，打印两个符号名并给出 `rm -rf` 修复提示后断言失败。这把「生成代码写错」这类隐蔽 bug 变成立即可见的硬失败。

**练习 2**：`KernelRuntimeCache` 的内存缓存键是 `dir_path`（一个字符串路径），而不是哈希摘要本身。这样做有什么好处？

> **答案**：`dir_path` 已经是「`cache/kernel.<name>.<digest>`」这样的全局唯一路径，本身就是摘要的函数；用它做键既唯一又可读。更重要的是，内存缓存与磁盘缓存**共用同一套寻址**——只要路径相同，内存命中与磁盘命中判定一致，不会出现「内存说命中、磁盘却不存在」的错位。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次「全流程追踪」：

**任务**：以 `dispatch` 内核为例，画出并标注一次 JIT 编译从「源码生成」到「`CUfunction` 就绪」的完整时序，并在磁盘上验证每个阶段的产物。

**建议步骤**：

1. 设置 `EP_JIT_CACHE_DIR=/tmp/deepep_demo EP_JIT_DEBUG=1`，清空目录后跑一次 dispatch。
2. 在日志里依次定位这几行，并在你的时序图上对齐：
   - `Generated kernel code:`（来自 [launch_runtime.hpp:43-44](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/launch_runtime.hpp#L43-L44)，对应「源码生成」）
   - `Running NVCC command:`（[compiler.hpp:234-235](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp#L234-L235)，对应「编译到 tmp」）
   - `Loading CUBIN:`（[kernel_runtime.hpp:26-27](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/kernel_runtime.hpp#L26-L27)，对应「加载」）
3. 用 `ls` 与 `cuobjdump -symbols` 确认 `cache/kernel.dispatch.<digest>/` 下 `kernel.cu` + `kernel.cubin` 齐全，且符号唯一。
4. **第二次**跑同样参数：日志里 `NVCC command` 与 `Loading CUBIN` 都应消失（内存缓存命中），从而量化「两级缓存」省下了多少时间。
5. 故意删掉 `kernel.cu`（只留 `kernel.cubin`）后再跑，观察会命中 `check_validity` 的「Corrupted JIT cache directory」报错，验证损坏检测生效。

**预期结果**：你能向别人讲清「内容寻址定位 → 内存缓存 → tmp 编译 → fsync → 原子 rename → 符号提取 → Driver API 加载」这条链上每一步对应的代码位置与磁盘现象。

> 待本地验证：步骤 2、4、5 需 GPU 多卡环境；步骤 1、3 的源码与磁盘结构部分可离线阅读理解。

## 6. 本讲小结

- **内容寻址缓存**：DeepEP 用「内核名 + 编译器签名 + 编译标志 + 源码（含头文件哈希）」拼串，经双 seed FNV-1a + split-mix64 得到 128 位摘要作为目录名，做到「同输入必命中、任何因素变化必重编」，绝不误用旧产物。
- **两级缓存**：`KernelRuntimeCache`（进程内 `unordered_map`）+ 磁盘目录；`build` 开头查内存、结尾查磁盘，重复调用零编译、零重复加载。
- **并发竞态处理**：多 rank 同路径编译时，各自编译到带 UUID 的私有 `tmp/` 目录，再用**整目录原子 rename** 到位；rename 失败即表示对端抢先，本 rank 丢弃自己的临时目录复用对端成果，竞态窗口近乎为零。
- **文件系统安全**：rename 前 `fsync_dir` 自底向上刷盘，保证分布式文件系统上对端可见；失败分支用 `safe_remove_all` 而非 `remove_all`，规避并发同父目录操作时的段错误。
- **cubin 加载**：用 `cuobjdump -symbols` 列符号，经「`STT_FUNC`+`STO_ENTRY` 白名单 / 辅助符号黑名单」过滤出**唯一**内核符号，再经 `cuModuleLoad` + `cuModuleGetFunction` 装载为 `CUfunction`；符号数 ≠ 1 即判损坏并给出修复提示。
- **`kernel.cu` 必须保留**：既是 `check_validity` 的完整性不变式，也支撑 PTX/SASS 调试与缓存可审计性。

## 7. 下一步学习建议

- 接下来学习 **u4-l4 内核启动框架：LaunchArgs 与设备能力探测**，了解 `CUfunction` 就绪后，`LaunchRuntime::launch` 如何用 `LaunchArgs`（grid/block/smem/cluster/cooperative/pdl）构造启动配置并真正把内核送上 GPU。
- 若想横向对照「缓存命中后如何被高频调用」，可回看 [csrc/kernels/elastic/dispatch.hpp:227-228](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/elastic/dispatch.hpp#L227-L228)，理解 `generate` → `build` 的调用频率与缓存收益的关系。
- 若你对底层 Driver API 感兴趣，可深读 [csrc/jit/handle.hpp](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/handle.hpp) 的 `construct_launch_config` 与 `launch_kernel`，那里展示了 cooperative/cluster/PDL 等 launch 属性如何附加到一次启动上（这是 u4-l4 的前奏）。
