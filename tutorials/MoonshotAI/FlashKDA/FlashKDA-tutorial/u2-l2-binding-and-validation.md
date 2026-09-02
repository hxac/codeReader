# u2-l2 pybind 入口：get_workspace_size 与 fwd 的校验链

## 1. 本讲目标

学完本讲，你应该能够：

1. **独立推导** `get_workspace_size` 在给定 `(T_total, H, N)` 下返回的字节数，包括 `total_tiles` 上界、每 tile 13824 字节的六段构成、以及 128 字节对齐的 `tile_prefix` 尾部缓冲。
2. **完整列出** `fwd` 中全部 `TORCH_CHECK` 校验规则，能预判任意非法输入会命中哪一行检查、得到哪条报错信息（校验顺序本身就是报错语义）。
3. **说清绑定层的三项布局/数值预处理**：`[B,T,H,D] → [B*T,H,D]` 的零拷贝 reshape、beta 的 `[T,H] → [H,T]` 拷贝式转置（为什么必须用 1D TMA）、以及 `gate_scale = lower_bound · log2(e)` 换底的意义。

本讲是上一讲（u2-l1，chunk 化算法骨架）的「工程侧对应」：torch_ref 告诉我们算法算什么，本讲告诉我们数据在进入 kernel 之前被怎样检查、怎样摆盘。

## 2. 前置知识

- **pybind11 与 `TORCH_CHECK`**：FlashKDA 用 pybind11 把 C++ 函数暴露给 Python（模块名 `flash_kda_C`）。`TORCH_CHECK(cond, msg)` 是 PyTorch 的运行时断言宏：条件不成立时抛出 `RuntimeError`（Python 侧 `except RuntimeError` 可捕获），并把调用栈里第一个失败的检查作为报错信息。**多个检查按代码顺序执行，先失败先报**——这一点本讲会反复利用。
- **workspace（工作区）**：承接 u1-l4 的结论，FlashKDA 是 K1（prepare）+ K2（recurrence）双 kernel 流水线，K1 把六个中间量（k_decayed、q_decayed、k_restored、g_total、INV、Mqk）写入一块全局显存缓冲，K2 再读。这块缓冲就是 workspace，由 Python 侧在调用前分配。
- **整数上取整技巧**：\(\lceil a/b \rceil = \lfloor (a + b - 1)/b \rfloor\)（正整数），以及「向上取整到 128 的倍数」写成 \(\lfloor (x + 127)/128 \rfloor \times 128\)。本讲源码里两个公式都用到了。
- **reshape / view / copy 语义**：PyTorch 中 `reshape` 对**连续**张量只是换形状的视图（`data_ptr` 不变、零拷贝）；`.t()` 转置是视图但结果**非连续**；`.contiguous()` 会触发一次真实的设备侧数据拷贝。
- **对数换底**：\(\ e^{g} = 2^{\,g \log_2 e}\ \)，其中 \(\log_2 e \approx 1.4426950408889634\)。GPU 上 `ex2`（计算 \(2^x\)）是单条快速指令，`exp` 则要靠换底后调用它——FlashKDA 把换底系数放在 host 侧预乘。

## 3. 本讲源码地图

| 文件 | 行数 | 本讲视角下的职责 |
|---|---|---|
| [csrc/flash_kda.cpp](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp) | 233 | 全部内容都是本讲对象：`get_workspace_size`、`fwd` 的校验链与布局预处理、`PYBIND11_MODULE` 注册 |
| [flash_kda/__init__.py](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/flash_kda/__init__.py) | 42 | Python 包装层：推导 `N`、分配 workspace、转发到 C++ |
| [csrc/fwd.h](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/fwd.h) | 27 | `launch_fwd` 模板声明，即校验通过后的下一站（u2-l3 的主角） |
| [csrc/smxx/fwd_kernel1.cuh](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh) | 586 | 本讲只引用两处消费侧证据：`gate_scale` 如何进 cumsum（L321-L323）、beta 的 smem 偏移（L345）与 `g_total` 的 ex2 形式（L353-L356） |

调用关系回顾（承接 u1-l4）：`flash_kda.fwd`（Python）→ `flash_kda_C.fwd`（本讲的 C++ `fwd`）→ `launch_fwd<...>`（u2-l3）→ K1/K2 kernel。

## 4. 核心概念与源码讲解

### 4.1 workspace 尺寸计算与对齐

#### 4.1.1 概念说明

workspace 是双 kernel 流水线的「传送带」：K1 是生产者、K2 是消费者。它必须在**两个 kernel 启动之前**就分配好、且大小精确已知——这就需要一个 host 侧的纯整数函数 `get_workspace_size`，而不是在 kernel 里动态扩容（CUDA 做不到）。

这个函数解决三个子问题：

1. **要开多少个 tile 槽位？** K1 按 tile 生产，槽位数 = `H × total_tiles`。
2. **每个 tile 占多少字节？** 六段中间量按固定布局排布，合计 13824 字节。
3. **varlen 模式的附加上下文放哪？** tile 前缀和数组（`N+1` 个 int32）放在整块缓冲的**尾部**。

为什么把它做成**导出给 Python 的独立接口**而不是 `fwd` 的内部细节？因为 Python 包装层要在调用 C++ 之前就 `torch.empty` 出这块缓冲（见 4.2.1 末尾的调用顺序），分配方和校验方需要共享同一个公式。

#### 4.1.2 核心流程

**第一步：tile 数上界。** varlen 模式下每条序列长度 \(\ell_i\) 各自占用 \(\lceil \ell_i/16 \rceil\) 个 tile，host 侧看不到各条长度（`cu_seqlens` 在显存里），于是取一个统一下界估计：

\[
\text{total\_tiles}_{ub} \;=\; \left\lceil \frac{T_{total}}{16} \right\rceil + N
\]

它确实是上界，因为

\[
\sum_{i=1}^{N}\left\lceil \frac{\ell_i}{16} \right\rceil \;\le\; \sum_{i=1}^{N}\left(\left\lfloor \frac{\ell_i}{16} \right\rfloor + 1\right) \;\le\; \left\lfloor \frac{T_{total}}{16} \right\rfloor + N \;\le\; \left\lceil \frac{T_{total}}{16} \right\rceil + N
\]

直观读法：**每条序列最多因为「尾巴不满 16」多占一个 tile**，所以上界 = 整体除法上取整 + 序列条数。

**第二步：每 tile 字节构成。**

| 段 | 形状 | dtype | 字节 |
|---|---|---|---|
| k_decayed / q_decayed / k_restored | 各 \(16 \times 128\) | bf16（2 字节） | \(3 \times 16 \times 128 \times 2 = 12288\) |
| g_total（存的是 \(\exp(\cdot)\) 形式，见 4.1.3） | \(128\) | fp32（4 字节） | \(128 \times 4 = 512\) |
| INV / Mqk | 各 \(16 \times 16\) | bf16 | \(2 \times 16 \times 16 \times 2 = 1024\) |

\[
\text{per\_tile\_bytes} = 12288 + 512 + 1024 = 13824
\]

**第三步：尾部前缀和缓冲。** varlen 下 K1 要把每条序列的 tile 数做前缀和（N+1 个端点：`[0, len₀, len₀+len₁, …]`）写进 workspace 尾部，供所有 block 二分查找自己属于哪条序列（详见 u2-l6）。按 128 字节向上对齐：

\[
\text{tile\_prefix\_bytes} = \left\lfloor \frac{4(N+1) + 127}{128} \right\rfloor \times 128
\]

**汇总：**

\[
\text{workspace} = H \times \text{total\_tiles}_{ub} \times 13824 \;+\; \text{tile\_prefix\_bytes}
\]

体量感受：\(T_{total}=4096,\ H=32,\ N=1\) 时为 \(32 \times 257 \times 13824 + 128 = 113{,}688{,}704\) 字节 ≈ **108.4 MiB**——这就是 u1-l5 说 workspace「可达数百 MiB」的来源。

**对齐不变量。** 三条 `static_assert` 把「每段大小都是 128 的倍数」固化在**编译期**：三段 bf16 矩阵各 4096 字节、g_total 512 字节、INV/Mqk 各 512 字节，全是 128 的倍数 ⇒ `per_tile_bytes` 是 128 的倍数 ⇒ 只要 tile 基地址对齐，**每个 tile 内六段的起始地址都天然落在 128 字节边界上**。这是后续 TMA 描述符按段切分 workspace 的前提（u2-l5）。而 `torch.empty` 的设备指针由 PyTorch 缓存分配器管理，对齐远高于 128 字节，所以代码无需再检查基地址。

#### 4.1.3 源码精读

先看整函数（仅 22 行）：

- [csrc/flash_kda.cpp:L5-L26](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L5-L26)：`get_workspace_size` 全文。纯 host 整数运算，不碰任何张量、不进显存。

逐段拆开：

- [csrc/flash_kda.cpp:L13-L14](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L13-L14)：注释明说「每条序列相对整除最多多出一个 tile」，`(T_total + CHUNK - 1) / CHUNK + N` 即上界公式。注意这里**无论 batched 还是 varlen 都用上界**——分配侧永远取保守值。
- [csrc/flash_kda.cpp:L16-L18](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L16-L18)：三条 `static_assert`，分别验证三类段的字节数是 128 的倍数（4096 % 128 == 0、512 % 128 == 0、512 % 128 == 0）。改动 CHUNK/D 时若破坏对齐，**编译期**直接报错而不是运行期写坏数据。
- [csrc/flash_kda.cpp:L20](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L20)：`per_tile_bytes = 3 * CHUNK * D * 2 + D * 4 + 2 * CHUNK * CHUNK * 2`，与 4.1.2 表格逐项对应（`3` 是三个 16×128 bf16 矩阵，`D*4` 是 fp32 的 g_total，`2*CHUNK*CHUNK*2` 是两个 16×16 bf16 矩阵）。
- [csrc/flash_kda.cpp:L22-L23](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L22-L23)：尾部 `tile_prefix` 缓冲，`(N+1)` 个 int32、向上取整到 128 的倍数。
- [csrc/flash_kda.cpp:L25](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L25)：`H * total_tiles * per_tile_bytes + tile_prefix_bytes`——主数据区按 `(head, tile)` 二维槽位展开，尾部再拼前缀和。

一个容易误解的细节：段名虽叫 `g_total`，但 K1 落盘的是 **exp 之后的值**。证据链：

- [csrc/smxx/fwd_kernel1.cuh:L353-L356](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L353-L356)：注释 `exp_g_total: compute exp(g_total) in smem before decay_apply`，随后把 smem 里的 g_total **原地覆盖**为 `ex2_approx_ftz_f32(x)`（即 \(2^x\)）。
- [csrc/smxx/fwd_kernel1.cuh:L550-L559](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L550-L559)：把这片 smem 以 `[D] float` TMA 存进 workspace。

所以 workspace 里的「g_total 段」存的是每通道一个 fp32 的**乘性整块衰减因子** \(\exp(g_{total})\)，这正是它用 fp32（`D * 4` 字节）而其他段用 bf16 的原因——K2 的状态更新走 fp32 累加路径（u3-l5、u3-l8 展开）。

再看 Python 侧如何消费这个函数：

- [flash_kda/__init__.py:L34-L38](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/flash_kda/__init__.py#L34-L38)：先从 `q.shape` 推出 `T_total`，再按 `cu_seqlens` 是否存在推出 `N`（存在时 `N = cu_seqlens.numel() - 1`，否则 `N = B`），然后 `torch.empty(get_workspace_size(T_total, H, N), dtype=torch.uint8, ...)`。注意 **N 的推导逻辑与 C++ 侧 L155/L159 必须保持一致**——这是同一合同在两侧的独立实现。

注册侧：

- [csrc/flash_kda.cpp:L227-L231](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L227-L231)：`get_workspace_size` 通过 `static_cast<int64_t(*)(int64_t,int64_t,int64_t)>` 以明确的函数指针类型注册；C++ 源码里的默认实参 `N = 1` 对 pybind 不可见，所以用 `py::arg("N") = 1` 在绑定层重新声明默认值。

#### 4.1.4 代码实践

**实践目标**：用 Python 复刻 workspace 公式，与 C++ 实现返回值对拍，验证你的推导逐字节正确。

**操作步骤**（示例代码，需在已安装 FlashKDA 的环境运行，结果待本地验证）：

```python
# ws_check.py —— 示例代码
from flash_kda_C import get_workspace_size  # 也可经 flash_kda.get_workspace_size 访问

CHUNK, D = 16, 128
def ws_formula(T_total, H, N):
    total_tiles = (T_total + CHUNK - 1) // CHUNK + N          # 上界
    per_tile = 3 * CHUNK * D * 2 + D * 4 + 2 * CHUNK * CHUNK * 2  # = 13824
    tile_prefix = ((N + 1) * 4 + 127) // 128 * 128            # 128 字节对齐
    return H * total_tiles * per_tile + tile_prefix

for (T_total, H, N) in [(4096, 32, 1), (8200, 96, 8), (128, 4, 2)]:
    mine, ref = ws_formula(T_total, H, N), get_workspace_size(T_total, H, N)
    assert mine == ref, (T_total, H, N, mine, ref)
    print(f"T_total={T_total} H={H} N={N}: {ref} B = {ref/1024**2:.1f} MiB")
```

**需要观察的现象**：三组参数全部 `assert` 通过；打印出的字节数与你手工按 4.1.2 公式算出的数一致。

**预期结果**（手工推导值，供对拍）：`(4096,32,1) → 113,688,704 B ≈ 108.4 MiB`；`(8200,96,8) → 691,421,312 B ≈ 659.4 MiB`（total_tiles = ⌈8200/16⌉+8 = 521）；`(128,4,2) → 553,088 B`。若对拍失败，优先检查你的 `tile_prefix` 是否忘了向上取整。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `total_tiles` 要 `+N`？batched 模式下这个上界浪费了多少？

**答案**：varlen 下每条序列的尾巴最多多占一个 tile（\(\lceil \ell_i/16\rceil \le \lfloor \ell_i/16\rfloor + 1\)），求和即得上界。batched 模式（每条等长 T）的精确 tile 数是 \(B \times \lceil T/16 \rceil\)（见 4.3.3 的 L180），上界与它之差 \(\lceil B T/16\rceil + B - B\lceil T/16\rceil\) 至多为 \(B\)；当 T 是 16 的倍数时恰好等于 \(B\)，即每条序列多预留 1 个 tile（\(13824 \times H\) 字节）。用 108 MiB 的例子说：batched 精确值只需 32×256×13824，上界分配了 32×257×13824，多出约 0.42 MiB——以少量显存换「一个公式同时服务两种模式」的简单性。

**练习 2**：`tile_prefix_bytes` 为什么是 `(N+1)` 个 int32 而不是 `N` 个？

**答案**：前缀和需要 N+1 个端点 `[0, len₀, len₀+len₁, …, T_total]`，第 i 条序列的 tile 范围由第 i 与第 i+1 个端点夹出；没有第 0 个端点 0，第一条序列就得特殊处理。

**练习 3**：如果把 CHUNK 从 16 改成 32，`static_assert` 会拦住什么？

**答案**：`CHUNK*CHUNK*2 = 2048`、`CHUNK*D*2 = 8192` 仍是 128 的倍数，对齐断言不拦；真正被破坏的是算法层——32×32 的 `(I+L)⁻¹` 无法再用 8×8 前代换精确完成（u3-l1），且门控累积和的动态范围翻倍可能超出 bf16 表示范围（u3-l8）。这说明对齐断言只守住「布局」这一层合同。

### 4.2 TORCH_CHECK 校验链

#### 4.2.1 概念说明

`fwd` 的前半部分是一条**顺序执行的合同检查流水线**。它的存在理由：kernel 一旦启动，错误就变成异步的、难以定位的（越界写可能沉默地污染数据）。绑定层把所有「能在 host 侧判定的违约」提前拦下，报错信息即合同文本。

理解这条链要抓住两个元规则：

1. **先到先得**：第一个失败的 `TORCH_CHECK` 决定报错内容。想预判报错，必须知道检查的**顺序**，而不只是集合。
2. **两阶段状态检查**：状态的 **dtype** 检查很早（L61-L79），**形状**检查很晚（L163-L174）——因为形状合同是 `[N, H, D, D]`，其中的 `N` 要等 `cu_seqlens` 处理完（L149-L160）才能确定。这是「检查依赖数据、数据依赖检查顺序」的典型结构。

还要注意一个**不在链上的合同**：workspace 只被检查 `is_cuda` 和 `is_contiguous`（L44-L47），**没有大小校验**。正确大小完全由 Python 包装层用 `get_workspace_size` 保证（[flash_kda/__init__.py:L38](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/flash_kda/__init__.py#L38)）。绕过包装层直接调 `flash_kda_C.fwd` 传一个过小的 workspace 不会报错，而是未定义行为（kernel 越界写）。

#### 4.2.2 核心流程

按执行顺序列出九组检查（行号均为 [csrc/flash_kda.cpp](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp)）：

| # | 检查组 | 行 | 内容 |
|---|---|---|---|
| 1 | 设备 + 连续性 | L44-L47 | q/k/v/g/beta/out/workspace 全部 `is_cuda` 且 `is_contiguous` |
| 2 | 六张主张量 dtype | L49-L54 | q/k/v/g/beta/out 全部 bf16 |
| 3 | 状态 dtype | L61-L79 | initial/final_state ∈ {bf16, fp32}、CUDA、连续；两者同给则 dtype 必须一致；任一是 fp32 则 `state_fp32 = true` |
| 4 | A_log / dt_bias | L81-L84 | 均为 fp32、CUDA、连续 |
| 5 | 维度 | L87-L92 | q/k/v/g/out 是 4D，beta 是 3D |
| 6 | 形状一致 | L100-L108 | k/v/g/out 逐维等于 q；beta = [B,T,H]；A_log = [H]；dt_bias = [H,D] |
| 7 | head_dim | L110 | `D == 128`（当前唯一支持） |
| 8 | cu_seqlens | L149-L157 | 提供 cu_seqlens 时：B==1、CUDA、**int64**、1D、`numel()-1 > 0`；同时定出 `N_val`（否则 `N_val = B`） |
| 9 | 状态形状 | L163-L174 | initial/final_state 必须是 `[N_val, H, D, D]` |

由「先到先得」可推出典型非法输入的归宿：

| 非法输入 | 命中 | 报错信息 |
|---|---|---|
| q 在 CPU 上（且是 fp32） | #1 | `all tensors must be on CUDA`（**不是** dtype 报错） |
| g 非连续 | #1 | `all tensors must be contiguous` |
| q 是 fp32 | #2 | `q must be bfloat16` |
| initial_state bf16 + final_state fp32 | #3 | `initial_state and final_state must have the same dtype` |
| A_log 是 fp64 | #4 | `A_log must be float32` |
| beta 是 4D | #5 | `beta must be [B, T, H]` |
| out 形状 ≠ q | #6 | `out must match q shape` |
| 全套一致的 D=64 输入 | #7 | `currently only supports D == 128` |
| 提供 cu_seqlens 但 B=2 | #8 | `B must be 1 when cu_seqlens is provided` |
| cu_seqlens 是 int32 | #8 | `cu_seqlens must be int64` |
| cu_seqlens 只有 1 个元素 | #8 | `cu_seqlens must have at least 2 elements` |
| initial_state 是 [N, H, D, 128]（最后一维错） | #9 | `initial_state must be [N, H, D, D]` |

注意「D=64」能一路走到 #7 的前提是**其他张量与 D=64 自洽**（dt_bias 是 [H,64] 等）；若 dt_bias 仍按 128 构造，会先在 #6 报 `dt_bias must be [H, D]`。

检查链之后是七分支模板分发（[L183-L213](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L183-L213)），那是 u2-l3 的主题，本讲不展开。

#### 4.2.3 源码精读

- [csrc/flash_kda.cpp:L28-L43](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L28-L43)：`fwd` 的完整签名。三个 optional 张量（`initial_state`/`final_state`/`cu_seqlens`）默认 `std::nullopt`，`lower_bound` 是 `double`、`scale` 是 `float`——Python 侧的 `None`/float 在这里落地。
- [csrc/flash_kda.cpp:L44-L47](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L44-L47)：第 1 组。一条 `TORCH_CHECK` 用 `&&` 串起七张张量的 `is_cuda`（第二条串 `is_contiguous`）——所以任何一张违约都得到同一条报错，**报错文本不指名哪张张量**，这是排错时要留心的。
- [csrc/flash_kda.cpp:L49-L54](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L49-L54)：第 2 组，六条独立检查一张一个报错，可精确定位。
- [csrc/flash_kda.cpp:L57-L79](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L57-L79)：第 3 组 + 三个布尔派生量 `has_state_in`/`has_state_out`/`state_fp32`。注意 L66/L73：**只要任一状态张量是 fp32，`state_fp32` 即为 true**——这个布尔之后会决定模板分发到 fp32 状态路径（u2-l3、u3-l6）。
- [csrc/flash_kda.cpp:L94-L98](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L94-L98)：从 q 读出 B/T_seq/H/D 并算 `T_total = B * T_seq`——后续所有形状检查的参照系。
- [csrc/flash_kda.cpp:L107-L110](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L107-L110)：A_log/dt_bias 形状与 D==128。`dt_bias must be [H, D]` 用的 D 是**用户传入的 D**，所以 D=64 且 dt_bias=[H,64] 时这条通过，最后由 L110 兜底拒绝。
- [csrc/flash_kda.cpp:L149-L160](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L149-L160)：第 8 组。先 `B == 1`，再依次 CUDA/int64/1D/非空，然后 `N_val = cu_seqlens_t.numel() - 1` 取设备指针；else 分支 `N_val = B`。与 [flash_kda/__init__.py:L36](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/flash_kda/__init__.py#L36) 的推导互为镜像。
- [csrc/flash_kda.cpp:L162-L174](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L162-L174)：第 9 组，状态形状 `[N_val, H, D, D]`。注意注释写的是 `[N, H, D, D]`——u1-l5 文档里的 `[B, H, V, K]`/`[N, H, V, K]` 在这里落地：batched 时 N_val=B，varlen 时 N_val=序列条数；V=K=D=128。
- [csrc/flash_kda.cpp:L219-L232](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L219-L232)：`PYBIND11_MODULE` 注册 `fwd` 与 `get_workspace_size`，`py::arg` 列表即 Python 侧的关键字参数名（`initial_state`/`final_state`/`cu_seqlens` 默认 `py::none()`）。

#### 4.2.4 代码实践

**实践目标**：构造至少 6 种非法输入，逐个调用 `flash_kda.fwd` 收集真实报错，整理成「规则 → 报错」对照表，验证 4.2.2 的顺序预测。

**操作步骤**（示例代码，待本地验证；合法输入的构造方式参照 [tests/test_fwd.py:L230-L234](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test_fwd.py#L230-L234)）：

```python
# invalid_inputs.py —— 示例代码
import torch
import flash_kda

B, T, H, D = 1, 64, 2, 128
DEV = "cuda"

def make_inputs(b=B, t=T, h=H, d=D):
    q = torch.randn(b, t, h, d, dtype=torch.bfloat16, device=DEV)
    return dict(
        q=q, k=torch.randn_like(q), v=torch.randn_like(q), g=torch.randn_like(q),
        beta=torch.randn(b, t, h, dtype=torch.bfloat16, device=DEV),
        scale=d ** -0.5, out=torch.empty_like(q),
        A_log=torch.randn(h, dtype=torch.float32, device=DEV),
        dt_bias=torch.randn(h, d, dtype=torch.float32, device=DEV),
        lower_bound=-5.0,
    )

def case(name, expect, mutate):
    kw = make_inputs()
    mutate(kw)
    return name, expect, kw

CASES = [
    case("q 为 fp32",        "q must be bfloat16",
         lambda kw: kw.update(q=kw["q"].to(torch.float32))),
    case("g 非连续",         "all tensors must be contiguous",
         lambda kw: kw.update(g=torch.randn(T, B, H, D, dtype=torch.bfloat16, device=DEV).permute(1, 0, 2, 3))),
    case("D=64（全套自洽）", "currently only supports D == 128",
         lambda kw: kw.update(**make_inputs(d=64), **{})),   # 见下方说明
    case("varlen 时 B=2",    "B must be 1 when cu_seqlens is provided",
         lambda kw: (kw.update(q=kw["q"].expand(2, T, H, D).contiguous()),   # 其余张量同样改 B=2
                     kw.update(cu_seqlens=torch.tensor([0, 2*T], dtype=torch.int64, device=DEV)))),
    case("状态 dtype 不匹配", "initial_state and final_state must have the same dtype",
         lambda kw: kw.update(
             initial_state=torch.randn(B, H, D, D, dtype=torch.bfloat16, device=DEV),
             final_state=torch.randn(B, H, D, D, dtype=torch.float32, device=DEV))),
    case("cu_seqlens 为 int32", "cu_seqlens must be int64",
         lambda kw: kw.update(cu_seqlens=torch.tensor([0, T], dtype=torch.int32, device=DEV))),
    # 附加用例
    case("q 在 CPU 上（且 fp32）", "all tensors must be on CUDA",
         lambda kw: kw.update(q=torch.randn(B, T, H, D, dtype=torch.float32))),
    case("out 形状不匹配",    "out must match q shape",
         lambda kw: kw.update(out=torch.empty(B, T, H, 64, dtype=torch.bfloat16, device=DEV))),
    case("cu_seqlens 仅 1 个元素", "cu_seqlens must have at least 2 elements",
         lambda kw: kw.update(cu_seqlens=torch.tensor([0], dtype=torch.int64, device=DEV))),
    case("initial_state 形状错", "initial_state must be [N, H, D, D]",
         lambda kw: kw.update(initial_state=torch.randn(B, H + 1, D, D, dtype=torch.bfloat16, device=DEV))),
]

if __name__ == "__main__":
    print("| 用例 | 预期命中的检查 | 实际报错（首行） |")
    print("|---|---|---|")
    for name, expect, kw in CASES:
        try:
            flash_kda.fwd(**kw)
            msg = "（未报错！）"
        except RuntimeError as e:
            msg = str(e).strip().splitlines()[0]
        print(f"| {name} | {expect} | {msg} |")
```

关于「D=64」用例的实现说明：`make_inputs(d=64)` 生成一整套 D=64 自洽输入（q/k/v/g/out/beta/dt_bias 全部按 64 构造），再整体替换 `kw`——示例中那行伪代码请展开为 `kw_new = make_inputs(d=64); kw.update(kw_new)`。「varlen 时 B=2」用例同理需要把七张张量都改成 B=2（`expand(...).contiguous()` 或直接用 `make_inputs(b=2)`）。

**需要观察的现象**：每个用例抛出的 `RuntimeError` 首行文本；特别对比「q 在 CPU 上（且 fp32）」得到的是 CUDA 报错而非 dtype 报错。

**预期结果**：实际报错与「预期命中的检查」列逐行一致，从而验证 4.2.2 的检查顺序表。若某个用例报出的不是预期检查，说明它先违反了排得更靠前的合同——回到顺序表定位即可。

#### 4.2.5 小练习与答案

**练习 1**：把一个形状正确但 dtype 为 fp16 的 `initial_state` 和 bf16 的 `final_state` 一起传入，报什么？

**答案**：第 3 组的 dtype 集合检查先拦：`initial_state must be bfloat16 or float32`（L64-L65）。若 initial_state 换成合法的 bf16，才会轮到 L77-L78 的「两者 dtype 一致」检查。

**练习 2**：为什么 workspace 没有大小校验？这安全吗？

**答案**：不查大小的直接原因是 Python 包装层总是用 `get_workspace_size` 分配（[flash_kda/__init__.py:L38](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/flash_kda/__init__.py#L38)），正常路径不可能传错；在 C++ 侧再查一遍需要在两处维护同一公式。代价是：绕过包装层直接调 `flash_kda_C.fwd` 且传小 workspace 时，是**无报错的未定义行为**（kernel 越界写显存）。「合同由调用方保证」是高性能绑定的常见取舍。

**练习 3**：`state_fp32` 为什么在 dtype 检查阶段（而非形状检查阶段）就要定下来？

**答案**：它是模板分发 `LAUNCH(HI, HO, FP32, VL)` 的编译期布尔之一（L184-L190），必须在分发前就绪；而形状检查只是运行时合同，可以放在 `N_val` 就绪之后的任意位置。dtype 决定「走哪条代码路径」，形状决定「数据对不对」——前者必须早于后者被固定。

### 4.3 布局预处理：reshape、beta 转置与 log2(e) 换底

#### 4.3.1 概念说明

校验通过后、启动 kernel 前，绑定层做三件「摆盘」工作，各自动机不同：

1. **零拷贝 reshape**：kernel 的全局内存视图按展平的 token 轴组织（u2-l5 将看到 gmem 布局是 `[H, T_total, D]`）。把 `[B,T,H,D]` 合并成 `[B*T,H,D]` 只是给同一块显存换一个形状描述，**一个字节都不搬**——前提是张量连续，而这已被第 1 组检查保证。这是「检查先行」的回报：后面的变换可以放心假设布局。
2. **beta 转置（唯一的真实拷贝）**：beta 原始布局 `[B,T,H]` 中，同一个 head 的相邻 token 相隔 H 个元素（跨步访问）；kernel 需要的是「某个 head 的连续 16 个 token」。转置成 `[H, T_total]` 后每 head 的 token 连续。源码注释点破了更深层的原因：**用 1D TMA 加载就没有 T 维对齐约束**——varlen 模式下 token 起点任意（序列长度不必是 16 的倍数），二维 tile TMA 的对齐要求无法满足；一维 TMA 只需 8 元素对齐，配合一个 smem 内偏移即可处理任意起点。
3. **换底预乘**：kernel 全程用 `ex2`（\(2^x\)）硬件指令代替 `exp`。由 \(\ e^{g} = 2^{\,g\log_2 e}\ \)，只要把门控值预先乘上 \(\log_2 e\)，cumsum 累加的就是「log2 域」的指数，之后一次 `ex2` 直接得到自然指数。这个系数在 host 侧每次调用只算一次，省下 device 侧每个元素的逐点乘法，也让 bit-exact 参考实现（u2-l1）少一处要复刻的指令。

#### 4.3.2 核心流程

```
校验通过
  │
  ├─ reshape（零拷贝视图）
  │    q/k/v/g/out: [B,T,H,D] → [T_total,H,D]
  │    beta:        [B,T,H]   → [T_total,H]
  │
  ├─ 指针提取（reinterpret_cast 到 cutlass::bfloat16_t*，纯位型桥接）
  │    gate_scale = lower_bound × log2(e)        ← 换底常量
  │
  ├─ beta 转置（真实拷贝，PyTorch 发起一次设备侧 elementwise kernel）
  │    beta_t = beta_2d.t().contiguous()          : [H, T_total]
  │
  ├─ 取 stream、状态裸指针（缺省为 nullptr）
  │
  └─ 计算 total_tiles：
       varlen  → (T_total+15)/16 + N      （上界，与分配侧一致）
       batched → N × ((T_seq+15)/16)      （精确值）
     → 进入七分支模板分发（u2-l3）
```

beta 转置的对齐细节（与 u3-l3 呼应，这里只给结论）：K1/K2 加载 beta 时按 8 元素对齐截断地址（`& ~7`），多读的头部元素用

\[ \text{beta\_smem\_offset} = (\,head \times T_{total} + bos + t \times 16\,) \bmod 8 \]

跳过——`bos + t*16` 正是转置后缓冲里该 tile 的线性起点。

#### 4.3.3 源码精读

- [csrc/flash_kda.cpp:L112-L118](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L112-L118)：七次 `reshape`。注释强调 `(contiguous, same data pointer)`——因为 L46 已保证连续，这些 reshape 全部是视图。
- [csrc/flash_kda.cpp:L120-L128](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L120-L128)：把 `at::BFloat16*` 逐个 `reinterpret_cast` 成 `cutlass::bfloat16_t*`（两者位布局一致，零开销桥接 PyTorch 与 CUTLASS 类型系统）；L128 计算 `gate_scale = float(lower_bound * 1.4426950408889634)`，那个字面量就是 \(\log_2 e\)。
- [csrc/flash_kda.cpp:L130-L132](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L130-L132)：beta 转置。源码注释原文即 `(1D TMA, no T alignment constraint)`——这是理解「为什么转置」的最权威依据。
- [csrc/flash_kda.cpp:L134-L142](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L134-L142)：workspace 裸指针、当前 stream（`at::cuda::getCurrentCUDAStream()`，保证与 PyTorch 侧流语义一致）、状态张量缺省时的 `nullptr` 哑指针。
- [csrc/flash_kda.cpp:L176-L181](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L176-L181)：`total_tiles` 的两种算法——varlen 用上界（与 4.1 的分配公式一致，K1 里超出实际 tile 数的 block 会 early return，见 u2-l6）；batched 用精确值 \(B \times \lceil T/16 \rceil\)（grid 不留空转 block）。分配取上界、启动取精确值，两者是「安全 ≥ 精确」的关系。

换底的消费侧证据（kernel 内）：

- [csrc/smxx/fwd_kernel1.cuh:L321-L323](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L321-L323)：`g_val = a_log_exp * (g_raw + dt)` 后紧接 `g_val = gate_scale * sigmoid_tanh_approx_f32(g_val)`。对照 u1-l2 的门控定义 \(g = lb \cdot \sigma(e^{A_{log}}(g_{raw} + dt_{bias}))\)，这里算出的其实是 \(g \cdot \log_2 e\)——**cumsum 直接累加在 log2 域**。
- [csrc/smxx/fwd_kernel1.cuh:L327-L330](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L327-L330)：`sum += g_val; g_smem[...] = sum`——每通道一个线程串行累加 16 行，无并行扫描。
- [csrc/smxx/fwd_kernel1.cuh:L353-L356](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L353-L356)：`ex2_approx_ftz_f32(x)` 即 \(2^x\)：因为 x 已含 \(\log_2 e\) 因子，结果等于 \(e^{g_{total}}\)。
- [csrc/smxx/fwd_kernel1.cuh:L345](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L345)：`beta_smem_offset = (head_idx * T_total + bos + local_t * CHUNK) & 7`——4.3.2 给出的偏移公式的代码原文，`& 7` 即 mod 8，正是 1D TMA「按 8 对齐加载 + 修剪头部」策略的另一半。

#### 4.3.4 代码实践

**实践目标**：用纯 PyTorch 区分「零拷贝视图」与「真实拷贝」，体会在 `flash_kda.fwd` 的调用路径中 beta 转置是绑定层唯一的数据搬运。

**操作步骤**（示例代码，`device="cuda"` 可改为 `"cpu"`，语义结论相同；运行结果待本地验证）：

```python
# layout_semantics.py —— 示例代码
import torch
B, T, H, D = 1, 64, 2, 128
q = torch.randn(B, T, H, D, dtype=torch.bfloat16, device="cuda")
beta = torch.randn(B, T, H, dtype=torch.bfloat16, device="cuda")

# ① reshape 是零拷贝视图（对应 C++ L113-L118）
q3 = q.reshape(B * T, H, D)
print("reshape 共享存储:", q3.data_ptr() == q.data_ptr())      # 预期 True

# ② beta 转置路径（对应 C++ L131）
b2 = beta.reshape(B * T, H)
bt = b2.t()
print("t() 后形状/连续性:", tuple(bt.shape), bt.is_contiguous())  # 预期 (H, B*T) False
btc = bt.contiguous()
print("contiguous() 发生拷贝:", btc.data_ptr() != beta.data_ptr()) # 预期 True

# ③（可选，需 GPU）用 profiler 观察这次拷贝是一次真实的 kernel
```

**需要观察的现象**：① 输出 `True`（视图）；② `.t()` 后 `is_contiguous()` 为 `False`，`.contiguous()` 返回**新存储**。

**预期结果**：与注释一致。若在 GPU 上用 `torch.profiler.profile` 包住 `flash_kda.fwd(...)`，除 K1/K2 外还能看到一个 elementwise 拷贝 kernel，即 beta 转置的开销（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：如果允许 beta 保持 `[T, H]` 不转置、直接给 kernel 跨步指针，会损失什么？

**答案**：TMA（SM90 的异步拷贝引擎）要求全局地址按拷贝宽度对齐且布局规则；`[T, H]` 下同一 head 的相邻 token 相距 H×2 字节，无法用一条 1D TMA 取连续 16 个 token，只能退化为普通 `ld.global` 逐元素加载，丢失异步批量传输能力——K1/K2 的流水线设计（u2-l6、u3-l3）都建立在 TMA 之上。

**练习 2**：`gate_scale` 若不在 host 预乘、而在 kernel 里对每个 cumsum 元素乘 \(\log_2 e\)，功能上可行吗？为什么仍然选择预乘？

**答案**：功能可行（数学等价），但要多做 \(T_{total} \times H \times D\) 次逐元素乘法，且 bit-exact 参考实现必须精确复刻这次乘法的位置与舍入（多一个「实现自由度」）。host 预乘把常量折叠为每次调用一次的标量乘法，device 侧指令流更短、数值路径更唯一。

**练习 3**：`total_tiles` 在 batched 模式下用精确值启动 grid，为什么不会与「分配侧用上界」冲突？

**答案**：workspace 的寻址公式是 `ws_idx = head × total_tiles + tile`（u1-l4），其中 `total_tiles` 用的是**启动时传入的那个值**（L188）：batched 下 grid 精确、写入不越界；上界分配只是预留了比写入更多的尾部空间（多出的槽位从不被写也不被读）。varlen 下两者同为上界，K1 中超过实际 tile 数的 block 直接 early return（u2-l6）。

## 5. 综合实践

**任务：给 `fwd` 的绑定层写一份「合同审计报告」。**

1. **校验侧**：运行 4.2.4 的 `invalid_inputs.py`，把输出表格与 4.2.2 的预测表逐行比对；如有不一致，解释是哪条更靠前的合同先被违反。
2. **尺寸侧**：运行 4.1.4 的 `ws_check.py`，并把三个例子的 `total_tiles`、`per_tile_bytes`、`tile_prefix_bytes` 分解值写进报告（练习手算与 C++ 对拍）。
3. **缺口侧**：对照 [flash_kda/__init__.py:L5-L33](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/flash_kda/__init__.py#L5-L33) 的 docstring 与 C++ 校验链，找出**「文档有约定但代码不检查」**的合同。至少应发现：(a) `lower_bound` 文档说期望在 `[-5.0, 0]`，代码不查（L39 的 `double lower_bound` 直接使用）；(b) workspace 大小不查（见 4.2.1）；(c) `T_seq`/`H` 为 0 等退化形状不查。为每条缺口注明：违约后果是什么、由谁兜底（如调用方约定、上游 FLA 封装）。
4. 把以上三部分整理成 `notes/binding-audit.md`（自建笔记目录），格式为三张表：`用例→报错`、`参数→字节分解`、`文档合同→是否强制`。

预期成果：一份能当「快速排错手册」用的审计报告——遇到 `flash_kda.fwd` 报错时按表索引即可定位违约规则；同时理解高性能绑定层「该校验的快查、可约定的不查」的取舍逻辑。

## 6. 本讲小结

- `get_workspace_size = H × (⌈T_total/16⌉ + N) × 13824 + ⌈4(N+1)/128⌉×128`：`+N` 吸收 varlen 每序列至多一个尾 tile；13824 = 三个 16×128 bf16 矩阵（12288）+ fp32 的 exp(g_total)（512）+ 两个 16×16 bf16 矩阵（1024）。
- 三条 `static_assert` 在编译期固化「每段 128 字节对齐」，保证 tile 内六段起始地址都落在 TMA 友好的边界上。
- `fwd` 的校验链按「设备/连续 → dtype → 状态 dtype → 维度/形状 → D==128 → cu_seqlens → 状态形状」顺序执行，**先失败先报**；状态检查拆成两阶段是因为形状里的 `N` 依赖 cu_seqlens 先定。
- workspace 只查 `is_cuda`/`is_contiguous`、**不查大小**，正确性由 Python 包装层用 `get_workspace_size` 分配来保证——绕过包装层传错大小是未定义行为。
- 绑定层三项预处理：reshape 是零拷贝视图（连续性由检查保证）；beta 转置 `[T,H]→[H,T]` 是唯一真实拷贝，动机是 1D TMA 无 T 对齐约束（8 元素对齐 + `& 7` 偏移）；`gate_scale = lower_bound·log2(e)` 让 kernel 全程用 `ex2` 硬件指令表达自然指数。
- 启动前 `total_tiles` 在 batched 用精确值、varlen 用上界，与分配侧「永远上界」构成「安全 ≥ 精确」的关系。

## 7. 下一步学习建议

本讲结束于 [csrc/flash_kda.cpp:L183-L213](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L183-L213) 的七分支分发宏——这正是下一讲 **u2-l3（模板参数分发）** 的入口：`LAUNCH(HI, HO, FP32, VL)` 里四个布尔如何映射到 `launch_fwd<D, HasStateIn, HasStateOut, StateFP32, IsVarlen>`（[csrc/fwd.h:L6-L27](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/fwd.h#L6-L27)）、为什么用编译期模板而不是运行时分支。之后再进入 **u2-l5**，看 workspace 的六段如何在 device 侧被 TMA 描述符精确切分——本讲的字节数公式将在那里被逐段消费。建议同步把 4.1.4 与 4.2.4 两个实践脚本留存，它们是 u2-l5 实践（workspace 布局对拍）的直接前置。
