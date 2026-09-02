# 第一次调用：flash_kda.fwd 的参数、形状与运行模式

## 1. 本讲目标

学完本讲，你应该能够：

1. **不看文档**就能正确构造一次 `flash_kda.fwd` 调用所需的全部输入张量（dtype、形状、设备、连续性一个都不错）。
2. 说清楚 **batched 模式**（`cu_seqlens=None`）与 **varlen 变长模式**（传入 `cu_seqlens`）在序列组织方式和状态张量形状上的差异。
3. 理解 workspace（中间结果缓冲区）是**如何被 Python 包装层自动分配的**：尺寸公式、`N` 的语义、为什么它可能大到上百 MiB。
4. 区分 **stateless / bf16 状态 / fp32 状态** 三种运行模式，知道每种模式下 `initial_state` 与 `final_state` 该怎么给、不该怎么给。

本讲是「入门单元」的最后一讲：u1-l3 装好了包、u1-l4 画出了调用链地图，本讲让你**真正把手指放到键盘上**，独立完成第一次（以及第一千次）正确的调用。

## 2. 前置知识

本讲假设你已读完 u1-l1 ~ u1-l4。需要用到的结论，这里用通俗语言快速回顾：

- **KDA 递推与状态**（u1-l2）：KDA 是带门控的 delta 规则线性注意力。它维护一个与序列长度无关的**状态矩阵** \( S \in \mathbb{R}^{V \times K} \)（V 是 value 维度、K 是 key 维度，本项目中 K = V = 128）。每个 token 先「擦除」旧记忆（乘以门控衰减）、再用 delta 修正「写入」新信息。`initial_state` 就是递推开始前的 \( S_0 \)，`final_state` 是整条序列处理完后的 \( S_T \)。
- **门控参数三件套**（u1-l2）：门控逐 key 通道计算，
  \[ g = \ell \cdot \sigma\big(e^{A_{log}} \cdot (g_{raw} + b_{dt})\big), \quad \ell = \text{lower\_bound} \]
  其中 \( A_{log} \) 是每个 head 一个标量（形状 `[H]`），\( b_{dt} \)（`dt_bias`）是每个 (head, 通道) 一个偏置（形状 `[H, K]`），\( g_{raw} \) 就是调用者传入的 `g` 张量（**激活前**的原始值）。`lower_bound` 限定门控只遗忘不放大。
- **beta 是 logits**（u1-l1）：调用者传入的是 sigmoid **之前**的 beta logits，kernel 内部自己做 sigmoid。同理 `g` 也是激活前的。这是 FlashKDA 与 FLA 集成时 `use_beta_sigmoid_in_kernel=True`、`use_gate_in_kernel=True` 的对应约定。
- **双 kernel 流水线**（u1-l4）：`flash_kda.fwd` 背后是 K1（prepare，按 tile 并行）+ K2（recurrence，按序列串行递推）两个 kernel，中间靠一块 **workspace** 传递 6 类中间张量。本讲只关心 workspace「多大、谁来分配」，其内部结构留到 u2-l8。
- **术语**：**logits** = 激活函数作用前的原始输出；**原地写入（in-place）** = 结果直接写进调用者预先分配的张量，不产生新张量；**contiguous** = 张量在内存中按行主序连续存储（`tensor.is_contiguous()` 为 True）。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
|---|---|---|
| [flash_kda/__init__.py](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/flash_kda/__init__.py#L1-L42) | Python 包装层（全项目唯一 Python 文件，42 行） | `fwd` 签名、`N` 的推导、workspace 自动分配 |
| [README.md](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/README.md#L79-L109) | 官方文档的 Kernel API 章节 | 权威参数表与约束清单 |
| [tests/test_fwd.py](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test_fwd.py#L222-L312) | 正确性测试 | batched 与 varlen 两种模式的**标准输入构造范例** |
| [csrc/flash_kda.cpp](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L1-L233) | pybind 入口（u1-l4 已见过地图） | 本讲只取两块：`get_workspace_size` 公式、`TORCH_CHECK` 校验规则 |

> csrc/flash_kda.cpp 的完整校验链与模板分发将在 u2-l2、u2-l3 深入精读；本讲只把它当作「参数表的机器可读版本」来用——每一条 README 约束都能在这份文件里找到对应的 `TORCH_CHECK`。

## 4. 核心概念与源码讲解

### 4.1 Python 包装层与 workspace 自动分配

#### 4.1.1 概念说明

FlashKDA 的 Python 层**薄到只有 42 行**，它做且只做三件事：

1. 提供 `flash_kda.fwd` 这个对用户友好的签名（可选参数带默认值）；
2. 根据输入形状**推导出三个标量**：`T_total`（token 总数）、`H`（head 数）、`N`（独立递推的序列条数）；
3. **自动分配 workspace**——一块 uint8 的一维大缓冲区，作为 K1 写、K2 读的「传送带」——然后调用 C++ 扩展 `flash_kda_C.fwd`。

为什么 workspace 不让用户管？因为它的尺寸公式和 kernel 的 tile 划分强耦合（CHUNK=16、D=128），暴露给用户只会增加出错面。包装层把它完全隐藏：**调用者永远不需要知道 workspace 的存在**。

#### 4.1.2 核心流程

`flash_kda.fwd(...)` 被调用后，Python 侧的执行流程：

```text
1. 从 q.shape 读出 B, T_seq, H
2. T_total = B * T_seq                         # token 总数（所有 batch 拼起来）
3. N = cu_seqlens.numel() - 1   (varlen 模式)   # 序列条数
   或 N = B                     (batched 模式)  # 每个 batch 元素就是一条序列
4. workspace = torch.empty(get_workspace_size(T_total, H, N), dtype=uint8)
5. _fwd_raw(q, k, v, g, beta, float(scale), out, workspace,
            A_log, dt_bias, lower_bound,
            initial_state, final_state, cu_seqlens)   # 进入 C++（u1-l4 的地图）
```

`N` 的语义是理解 workspace 尺寸的钥匙：**N = kernel 2 需要独立递推的序列条数**。batched 模式下每个 batch 元素独立递推，所以 N=B；varlen 模式下 `cu_seqlens` 有 N+1 个边界点，切出 N 条序列。

workspace 尺寸由 C++ 侧的 `get_workspace_size` 计算（Python 通过 pybind 直接调用同一个函数）：

\[
\text{total\_tiles} = \left\lceil \frac{T_{total}}{16} \right\rceil + N
\]

\[
\text{per\_tile\_bytes} = \underbrace{3 \times (16 \times 128 \times 2)}_{\text{k/q decayed + k\_restored}} + \underbrace{128 \times 4}_{\text{g\_total}} + \underbrace{2 \times (16 \times 16 \times 2)}_{\text{INV + Mqk}} = 13824
\]

\[
\text{size} = H \times \text{total\_tiles} \times 13824 + \Big\lceil \tfrac{(N+1) \times 4}{128} \Big\rceil \times 128
\]

三个注意点：

- `total_tiles` 是 **varlen 下的上界**：每条序列的长度不是 16 的倍数时各会多出一个不满的 tile，最坏情况每序列多 1 个 tile，所以加 `N`。
- 尾部的 `tile_prefix`（N+1 个 int32，按 128 字节对齐）只有 varlen 模式才真正被 kernel 写入（K1 用它做前缀和 + 二分查找，见 u2-l6），但公式对两种模式统一生效，batched 也会多给这一小段。
- 尺寸与 \( T_{total} \times H \) **线性相关**且系数不小（每 tile 每 head 13824 字节），所以长序列 + 多 head 时 workspace 轻松超过数百 MiB——这不是 bug，是「用显存换并行度」的设计选择。

#### 4.1.3 源码精读

先看完整的包装层（这个文件总共就这么多）：

[flash_kda/__init__.py:1-5](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/flash_kda/__init__.py#L1-L5) —— 从编译出的 C++ 扩展 `flash_kda_C` 导入底层 `fwd` 与 `get_workspace_size`，并定义 Python 侧 `fwd` 签名（可选参数 `initial_state` / `final_state` / `cu_seqlens` 默认 `None`）：

```python
import torch
from flash_kda_C import fwd as _fwd_raw, get_workspace_size


def fwd(q, k, v, g, beta, scale, out, A_log, dt_bias, lower_bound, initial_state=None, final_state=None, cu_seqlens=None):
```

[flash_kda/__init__.py:34-38](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/flash_kda/__init__.py#L34-L38) —— 形状推导与 workspace 分配，这是本模块的核心四行：

```python
    B, T_seq, H = q.shape[0], q.shape[1], q.shape[2]
    T_total = B * T_seq
    N = cu_seqlens.numel() - 1 if cu_seqlens is not None else B

    workspace = torch.empty(get_workspace_size(T_total, H, N), dtype=torch.uint8, device=q.device)
```

注意 `N` 的双态语义（varlen 取 `cu_seqlens` 长度减一，batched 取 B），以及 workspace 是 `torch.empty`（不初始化——kernel 会先写后读，无需清零）。

[flash_kda/__init__.py:40-41](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/flash_kda/__init__.py#L40-L41) —— 把所有参数（含 workspace）转发给 C++ 扩展，`scale` 在此处被转成 Python float：

```python
    _fwd_raw(q, k, v, g, beta, float(scale), out, workspace, A_log, dt_bias, lower_bound,
             initial_state=initial_state, final_state=final_state, cu_seqlens=cu_seqlens)
```

再看尺寸公式本身：

[csrc/flash_kda.cpp:5-26](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L5-L26) —— `get_workspace_size` 全文。`CHUNK=16`、`D=128` 写死为常量；注释说明 `+ N` 是 varlen 的上界补偿；`per_tile_bytes` 的三项对应 6 段中间张量（3 个 16×128 的 bf16 矩阵、128 个 fp32 标量、2 个 16×16 的 bf16 矩阵）；三行 `static_assert` 保证每段都 128 字节对齐（TMA 访存的硬性要求）：

```cpp
int64_t get_workspace_size(int64_t T_total, int64_t H, int64_t N = 1) {
    constexpr int CHUNK = 16;
    constexpr int D = 128;
    // Upper bound: each of N sequences adds at most 1 extra tile vs floor division
    int64_t total_tiles = (T_total + CHUNK - 1) / CHUNK + N;
    ...
    int64_t per_tile_bytes = 3 * CHUNK * D * 2 + D * 4 + 2 * CHUNK * CHUNK * 2;
    // Trailing buffer for the tile prefix-sum (N+1 int32), 128-byte aligned.
    int64_t tile_prefix_bytes = ((N + 1) * 4 + 127) / 128 * 128;
    return H * total_tiles * per_tile_bytes + tile_prefix_bytes;
}
```

[csrc/flash_kda.cpp:227-231](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L227-L231) —— pybind 注册：`get_workspace_size` 也是公开导出的函数，`N` 有默认值 1（包装层总是会显式传 N，默认值只是给直接调用者的兜底）。

一个值得记录的工程细节：**每次调用 `flash_kda.fwd` 都会新分配一个 workspace**。由于 PyTorch 的 caching allocator 会复用同尺寸的显存块，重复同形状调用时这次 `torch.empty` 几乎没有实际开销；但公开 API 不允许外部传入 workspace——pybind 层的 `fwd` 虽然有 `workspace` 参数，Python 包装层没有暴露它。想复用 workspace 属于内部用法（直接 import `flash_kda_C`），正式代码不建议依赖。

#### 4.1.4 代码实践：workspace 尺寸对拍

**实践目标**：验证你真的理解了尺寸公式——用手算（Python 复现公式）与 kernel 侧的 `get_workspace_size` 对拍。

**操作步骤**：保存以下脚本并运行（示例代码，实践脚本）：

```python
# ws_probe.py —— 手工复现 workspace 公式，与 C++ 实现对拍
import math
from flash_kda_C import get_workspace_size

CHUNK, D = 16, 128
PER_TILE = 3 * CHUNK * D * 2 + D * 4 + 2 * CHUNK * CHUNK * 2   # = 13824

def my_ws_size(T_total, H, N):
    total_tiles = (T_total + CHUNK - 1) // CHUNK + N
    tile_prefix = ((N + 1) * 4 + 127) // 128 * 128
    return H * total_tiles * PER_TILE + tile_prefix

cases = [
    ("本讲综合实践形状 (T=4096,H=32,batched)", 4096, 32, 1),
    ("test_fwd.py 的形状 (T=8192,H=96,batched)", 8192, 96, 1),
    ("test_fwd_varlen 的形状 (T=8192,H=96,N=6)",  8192, 96, 6),
]
for name, T, H, N in cases:
    mine, ref = my_ws_size(T, H, N), get_workspace_size(T, H, N)
    print(f"{name}: 手算={mine:,}  C++={ref:,}  一致={mine == ref}"
          f"  ({mine / 1024 / 1024:.1f} MiB)")
```

**需要观察的现象**：三行 `一致=True`；以及 workspace 的量级——即使本讲综合实践这种中等形状（T=4096, H=32）也约 108 MiB，而测试用的 T=8192、H=96 达到 650 MiB 左右。

**预期结果**（按公式手算，待本地验证）：

| T_total | H | N | total_tiles | 字节数 | 约合 |
|---|---|---|---|---|---|
| 4096 | 32 | 1 | 257 | 113,688,704 | 108.4 MiB |
| 8192 | 96 | 1 | 513 | 680,804,480 | 649.3 MiB |
| 8192 | 96 | 6 | 518 | 687,440,000 | 655.6 MiB |

（最后一行的 `total_tiles` 是 varlen 上界 \( 512 + 6 \)，比 batched 同长度多 5 个 tile 的冗余。）

#### 4.1.5 小练习与答案

**练习 1**：batched 模式下 B=4、每条序列 T=100、H=8，workspace 的 `total_tiles` 是多少？实际真正会被计算的 tile 有多少个？

答案：`T_total = 400`，上界 `total_tiles = ⌈400/16⌉ + 4 = 25 + 4 = 29`；但每条序列实际 tile 数为 `⌈100/16⌉ = 7`，真实计算 `4 × 7 = 28` 个 tile。上界多算的 1 个 tile 是公式对「每序列最多多 1 个尾 tile」的统一补偿（batched 下 `fwd` 内部其实算了精确值，见 [csrc/flash_kda.cpp:176-181](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L176-L181)，workspace 按上界分配不影响正确性）。

**练习 2**：为什么 `get_workspace_size` 里那三行 `static_assert` 检查 128 字节对齐是硬性要求，而不是性能建议？

答案：workspace 会被切成 6 段数组分别建 TMA 描述符（u2-l5），TMA 对全局内存基地址有对齐要求；若某段尺寸不是对齐字节的整数倍，下一段的起始地址就不对齐，TMA 拷贝会非法。所以这是正确性约束，编译期用 `static_assert` 拦截。

**练习 3**：调用者能否通过 `flash_kda.fwd` 复用一块预先分配好的 workspace？

答案：不能。Python 包装层在函数内部 `torch.empty` 分配后立即转发，签名里没有 workspace 参数；pybind 层虽有该参数但那是给包装层用的内部接口。得益于 caching allocator，每次分配的实际开销很小。

### 4.2 输入张量规格表：dtype、形状与语义

#### 4.2.1 概念说明

`flash_kda.fwd` 一共有 13 个参数。它们可以分为四组，每组的学习方式不同：

| 组 | 参数 | 学习要点 |
|---|---|---|
| 序列输入 | `q` `k` `v` `g` `beta` | 形状 `[B, T, H, ·]`、全部 bf16、**g/beta 传激活前 logits** |
| 标量与门控配置 | `scale` `A_log` `dt_bias` `lower_bound` | scale 是 Python float；后三者控制门控激活曲线 |
| 输出缓冲 | `out` | 调用者**预分配**，kernel 原地写入 |
| 可选状态与变长 | `initial_state` `final_state` `cu_seqlens` | 决定运行模式（见 4.3） |

最容易踩的坑有三个：

1. **「logits 约定」**：`g` 和 `beta` 传的是激活前的值，sigmoid / 门控激活都在 kernel 内部完成。如果你已经在外面做过 `torch.sigmoid(beta)`，结果就错了。
2. **`out` 不是返回值**：`flash_kda.fwd` 返回 `None`，结果写在传入的 `out` 张量里（和 `final_state` 一样是输出缓冲）。这是 CUDA kernel 库的常见风格，避免每次调用都分配新张量。
3. **D=128 硬约束**：K（key 维）与 V（value 维）目前**只支持 128**，其他值会在校验阶段直接报错。

另外一个「不需要做」的事：**q/k 不需要预先 L2 归一化**。kernel 1 内部会对 q/k 逐行做 L2 归一化（见 [csrc/smxx/fwd_kernel1.cuh:265-296](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L265-L296) 的 `rsqrtf` 归一化段），调用者传原始值即可。测试脚本里的 `F.normalize(...)` 是为了让参考实现拿到与 kernel 相同的输入做 bit-exact 对拍，属于测试惯例，不是调用要求。

#### 4.2.2 核心流程

构造一次调用的思考清单（按顺序核对即可）：

```text
□ 1. 定形状：B, T, H, D(=128)
□ 2. 序列输入 5 件套：全部 bf16、CUDA、contiguous
     q/k/g: [B, T, H, 128]    v: [B, T, H, 128]    beta: [B, T, H]（少最后一维！）
□ 3. 门控三件套：A_log [H] fp32；dt_bias [H, 128] fp32；lower_bound 标量（[-5.0, 0]）
□ 4. scale：Python float，常见取 1/sqrt(128)
□ 5. 输出缓冲：out = torch.empty_like(v)（或 zeros）
□ 6. 选运行模式（→ 4.3）：要不要 initial_state / final_state？要不要 cu_seqlens？
□ 7. 调用 flash_kda.fwd(q, k, v, g, beta, scale, out, A_log=..., dt_bias=..., lower_bound=...)
```

其中每条「应当」都能在 C++ 校验链里找到对应的 `TORCH_CHECK`——README 的参数表是「人读版」，`TORCH_CHECK` 是「机器执行版」，两者一一对应。

#### 4.2.3 源码精读

先看权威参数表。[README.md:90-104](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/README.md#L90-L104) 给出全部 13 个参数的 dtype/形状/说明；[README.md:106-109](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/README.md#L106-L109) 是约束清单（K=V=128、状态 dtype 一致、varlen 时 B=1）。综合 docstring 与 README，整理成中文规格表：

| 参数 | dtype | 形状 | 语义与注意点 |
|---|---|---|---|
| `q` | bf16 | `[B, T, H, K]` | 查询；kernel 内部做 L2 归一化并乘 `scale` |
| `k` | bf16 | `[B, T, H, K]` | 键；kernel 内部做 L2 归一化 |
| `v` | bf16 | `[B, T, H, V]` | 值；不归一化 |
| `g` | bf16 | `[B, T, H, K]` | **激活前**门控原始值，逐 key 通道 |
| `beta` | bf16 | `[B, T, H]` | **激活前** beta logits，kernel 内做 sigmoid |
| `scale` | float | 标量 | 缩放系数，通常 `1/√D` |
| `out` | bf16 | `[B, T, H, V]` | **预分配的输出缓冲**，原地写入 |
| `A_log` | fp32 | `[H]` | 门控对数幅度（每 head 一个） |
| `dt_bias` | fp32 | `[H, K]` | 门控偏置（每 head 每 channel 一个） |
| `lower_bound` | float | 标量 | 门控下界，取值范围 `[-5.0, 0]` |
| `initial_state` | bf16/fp32/None | `[B, H, V, K]` 或 `[N, H, V, K]` | 初始状态 \( S_0 \)；`None` = 从零开始 |
| `final_state` | bf16/fp32/None | 同上 | **预分配的**最终状态输出缓冲 |
| `cu_seqlens` | int64 | `[N+1]` | 变长序列累积长度；提供时 B 必须为 1 |

然后看「机器执行版」的校验链，每条规则右边是它在源码中的位置：

[csrc/flash_kda.cpp:44-54](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L44-L54) —— 第一道关：所有张量必须在 CUDA 上、必须 contiguous；`q/k/v/g/beta/out` 六个张量必须全是 bf16：

```cpp
    TORCH_CHECK(q.is_cuda() && ... , "all tensors must be on CUDA");
    TORCH_CHECK(q.is_contiguous() && ... , "all tensors must be contiguous");
    TORCH_CHECK(q.dtype() == torch::kBFloat16, "q must be bfloat16");
```

[csrc/flash_kda.cpp:81-92](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L81-L92) —— `A_log`/`dt_bias` 必须 fp32 且 contiguous；维度检查：`q/k/v/g/out` 是 4 维、`beta` 是 **3 维** `[B, T, H]`（beta 少最后一维是最常见的形状错误之一）。

[csrc/flash_kda.cpp:100-110](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L100-L110) —— 形状一致性：`k/v/g/out` 的 sizes 必须与 `q` 完全相同，`beta` 的前三维必须匹配 `[B, T, H]`；`A_log` 必须是 `[H]`、`dt_bias` 必须是 `[H, D]`；最后是硬性约束 `D == 128`：

```cpp
    TORCH_CHECK(k.sizes() == q.sizes(), "k must match q shape");
    ...
    TORCH_CHECK(D == 128, "currently only supports D == 128");
```

最后看**标准构造范例**。[tests/test_fwd.py:228-247](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test_fwd.py#L228-L247) —— `test_fwd` 的输入构造与调用（batched 模式）。注意四个细节：`F.normalize` 预归一化（测试惯例，见 4.2.1）；`A_log`/`dt_bias` 用 `torch.rand`（即取值 [0,1) 的 fp32）；`initial_state` 用 `arange` 填充再转 bf16（大数值也能跑，正好考验精度）；`out_kernel` 与 `final_state_kernel` 都是**先分配再传入**：

```python
    q = F.normalize(torch.randn((B, T, H, D), ...), p=2, dim=-1).to(torch.bfloat16)
    ...
    beta = torch.randn((B, T, H), dtype=torch.bfloat16, device='cuda')
    A_log = torch.rand(H, dtype=torch.float32, device='cuda')
    dt_bias = torch.rand(H, D, dtype=torch.float32, device='cuda')
    initial_state = torch.arange(H * D * D, ...).reshape(1, H, D, D).to(torch.bfloat16)
    scale = 1.0 / math.sqrt(D)

    final_state_kernel = torch.zeros_like(initial_state)
    out_kernel = torch.zeros_like(q)
    flash_kda.fwd(q, k, v, g, beta, scale, out_kernel,
                  A_log=A_log, dt_bias=dt_bias, lower_bound=LOWER_BOUND,
                  initial_state=initial_state.clone(), final_state=final_state_kernel)
```

顺带一提：`test_fwd` 没有包 `torch.inference_mode()` 也能跑——直接调用 `flash_kda.fwd` 不强制 inference mode（README 里要求 inference_mode 的是走 FLA `chunk_kda` 分发的用法，见 u3-l11）。

#### 4.2.4 代码实践：最小调用 + 原地写入验证

**实践目标**：完成一次最小合法调用，并用「NaN 预填充」验证 `out` 确实是被原地写入的。

**操作步骤**（示例代码，实践脚本）：

```python
# mini_call.py —— 第一次调用 flash_kda.fwd
import torch, math, flash_kda

torch.manual_seed(0)
B, T, H, D = 1, 64, 2, 128          # 最小形状：T 取 16 的倍数，避开尾块话题
dev = 'cuda'

q = torch.randn(B, T, H, D, dtype=torch.bfloat16, device=dev)
k = torch.randn(B, T, H, D, dtype=torch.bfloat16, device=dev)
v = torch.randn(B, T, H, D, dtype=torch.bfloat16, device=dev)
g = torch.randn(B, T, H, D, dtype=torch.bfloat16, device=dev)
beta = torch.randn(B, T, H, dtype=torch.bfloat16, device=dev)
A_log = torch.rand(H, dtype=torch.float32, device=dev)
dt_bias = torch.rand(H, D, dtype=torch.float32, device=dev)

out = torch.full((B, T, H, D), float('nan'), dtype=torch.bfloat16, device=dev)  # 预填 NaN

flash_kda.fwd(q, k, v, g, beta, 1.0 / math.sqrt(D), out,
              A_log=A_log, dt_bias=dt_bias, lower_bound=-5.0)
torch.cuda.synchronize()

print("out 中残留 NaN 个数:", torch.isnan(out.float()).sum().item())   # 预期 0
print("out: mean=%.4f  absmax=%.4f" % (out.float().mean(), out.float().abs().max()))

# 确定性检查：同样的输入再跑一次，应当逐位一致
out2 = torch.full_like(out, float('nan'))
flash_kda.fwd(q, k, v, g, beta, 1.0 / math.sqrt(D), out2,
              A_log=A_log, dt_bias=dt_bias, lower_bound=-5.0)
torch.cuda.synchronize()
print("两次调用逐位一致:", torch.equal(out, out2))
```

**需要观察的现象**：NaN 计数为 0（证明每个元素都被 kernel 写过，`out` 是输出缓冲而非输入）；`out` 的数值是有界的小数值（q/k 被 L2 归一化后点积量级有限）；两次调用 `torch.equal` 为 True。

**预期结果**：三条都符合即通过（逐位一致性源于 kernel 是确定性的固定网格计算；本脚本未在本讲写作环境中运行，待本地验证）。

**延伸**（为 u2-l2 热身）：把 `q` 换成 fp32 再调用一次，观察报错信息 `q must be bfloat16`；把 `beta` 造成 4 维，观察 `beta must be [B, T, H]`——每条报错都来自你在 4.2.3 里读到的 `TORCH_CHECK`。

#### 4.2.5 小练习与答案

**练习 1**：同事写了一层 KDA，外面已经算过 `beta = torch.sigmoid(beta_logits)`，直接把结果传给 `flash_kda.fwd` 会怎样？

答案：不会报错（形状 dtype 都合法），但**结果是错的**：kernel 内部会再做一次 sigmoid，等效于双重激活，写入强度被压缩到约 0.5~0.73 区间。这正是「logits 约定」的隐蔽坑——错误是数值性的而非报错性的。正确做法是传 `beta_logits` 本身。

**练习 2**：为什么 `A_log` 的形状是 `[H]`（每 head 一个标量）而 `dt_bias` 是 `[H, K]`（每通道一个）？

答案：由门控公式 \( g = \ell \cdot \sigma(e^{A_{log}} \cdot (g_{raw} + b_{dt})) \) 的广播结构决定：\( e^{A_{log}} \) 是该 head 所有通道共享的幅度缩放，而 \( b_{dt} \) 是逐通道的偏移。二者一栏看，`g [B,T,H,K] + dt_bias [H,K]` 逐通道相加后，再被每 head 一个的 \( e^{A_{log}} \) 缩放。

**练习 3**：`out` 可以用 `torch.empty` 而不是 `torch.zeros` 分配吗？`final_state` 呢？

答案：都可以。`out` 与 `final_state` 都是纯输出缓冲：kernel 对 `out` 的每个有效 token 位置都会写入（无状态输入时状态从零开始，也是 kernel 内部初始化，与缓冲区旧值无关）；`final_state` 在提供时会被完整写入。mini_call.py 用 NaN 预填充验证的就是这一点。

### 4.3 三种运行模式：stateless / bf16 状态 / fp32 状态（含 varlen）

#### 4.3.1 概念说明

`initial_state` 与 `final_state` 两个可选参数的组合决定了**运行模式**。先看最常用的三种：

1. **stateless（无状态）**：两者都不传。状态从零开始（\( S_0 = 0 \)），算完即弃。适用于纯训练前向、不需要跨段携带记忆的场景。
2. **bf16 状态**：两者都传 bf16 张量。初始状态以 bf16 直通加载进 kernel、最终状态以 bf16 写出——这是与 FLA `chunk_kda`（`transpose_state_layout=True`）互操作时的默认精度。
3. **fp32 状态**：两者都传 fp32 张量。kernel 会在入口把 fp32 状态转换成内部 bf16 计算、在出口把结果转回 fp32 写出，**外部看到的进出精度都是 fp32**。适用于对状态保真度要求高的场景（如长链路递推、推理缓存状态）。

两个关键规则：

- **进出的 dtype 必须一致**：`initial_state` 与 `final_state` 都提供时，dtype 必须相同（一个 bf16 一个 fp32 会报错）。内部状态始终以 bf16 计算（u1-l1 的设计决策），fp32 模式只是进出口的转换包装。
- **只传一个是合法的**：例如只要 `final_state` 不要 `initial_state`（从零开始但想知道结束状态——解码到段尾时缓存状态正是这个用法）；或只要 `initial_state` 不要 `final_state`（带着历史状态继续算、不关心结束状态）。

加上「只传一个」的各种组合，`(有/无 initial_state) × (有/无 final_state) × (bf16/fp32)` 在数学上有 7 种合法组合（全无 + 3 种「都传」之外还有 4 种「只传一个」……准确地说：全无 1 种 + 都传 2 种 + 只传 out 2 种 + 只传 in 2 种 = 7 种）。这 7 种组合在 C++ 侧各对应一个模板实例（u2-l3 的主题），本讲只需知道：**模式由你传不传、传什么 dtype 决定，不需要显式开关**。

**varlen 变长模式**是另一个正交的开关：传 `cu_seqlens`（int64、`[N+1]`、在 CUDA 上、首元素为 0）就把 `[1, T, H, ·]` 的输入切成 N 条变长序列，每条独立递推、独立拥有自己的状态。此时：

- `B` 必须为 1（变长信息完全由 `cu_seqlens` 表达）；
- `T` 是所有序列的**总长度**；
- 状态形状从 `[B, H, V, K]` 变为 **`[N, H, V, K]`**——第一条轴的语义从 batch 变成「序列」。

#### 4.3.2 核心流程

模式选择可以画成一棵小决策树：

```text
需要跨调用携带状态吗？
├─ 否 → stateless：initial_state=None, final_state=None
└─ 是 → 状态精度？
    ├─ 与 FLA 互操作 / 显存敏感 → bf16：两者都给 [·, H, 128, 128] bf16
    └─ 高保真 → fp32：两者都给 [·, H, 128, 128] fp32

输入是等长 batch 还是拼接的变长序列？
├─ 等长 batch → cu_seqlens=None，状态第 0 维 = B
└─ 变长拼接 → cu_seqlens=[0, l₁, l₁+l₂, ...]（int64, CUDA），B=1，状态第 0 维 = N
```

状态张量的轴语义（承接 u1-l2 的 \( S \in \mathbb{R}^{V \times K} \)）：

\[ \text{state}[\,b,\ h,\ v,\ k\,] = S^{(b,h)}_{v,k} \]

即第 2 轴是 **V（value 维）**、第 3 轴是 **K（key 维）**。因为本项目 K = V = 128，从形状上看不出区别，但语义不能记反——这正是 FLA 集成时 `transpose_state_layout=True` 所匹配的布局。

#### 4.3.3 源码精读

[csrc/flash_kda.cpp:57-79](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L57-L79) —— 状态张量的 dtype 校验：允许 bf16 或 fp32；只要任意一个是 fp32，整体就按 fp32 模式（`state_fp32 = true`）；**两者都提供时 dtype 必须一致**：

```cpp
    if (has_state_in) {
        auto& is = initial_state.value();
        TORCH_CHECK(is.dtype() == torch::kBFloat16 || is.dtype() == torch::kFloat32, ...);
        if (is.dtype() == torch::kFloat32) state_fp32 = true;
    }
    ...
    if (has_state_in && has_state_out) {
        TORCH_CHECK(initial_state->dtype() == final_state->dtype(),
                     "initial_state and final_state must have the same dtype");
    }
```

[csrc/flash_kda.cpp:162-174](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L162-L174) —— 状态形状校验：统一要求 `[N, H, D, D]`，其中第 0 维必须是 `N_val`——batched 模式下 `N_val = B`，varlen 模式下 `N_val = cu_seqlens.numel() - 1`。这一条就是「batched 状态 `[B,H,V,K]`、varlen 状态 `[N,H,V,K]`」规则的机器版：

```cpp
    // Validate state shapes: always [N, H, D, D]
    if (has_state_in) {
        TORCH_CHECK(is.dim() == 4, "initial_state must be [N, H, D, D]");
        TORCH_CHECK(is.size(0) == N_val && is.size(1) == H && is.size(2) == D && is.size(3) == D, ...);
    }
```

[csrc/flash_kda.cpp:149-157](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L149-L157) —— `cu_seqlens` 的五条校验：提供时 `B == 1`、必须在 CUDA 上、必须 int64（`torch.long`）、必须 1 维、至少 2 个元素（即 N ≥ 1）：

```cpp
    if (is_varlen) {
        TORCH_CHECK(B == 1, "B must be 1 when cu_seqlens is provided");
        TORCH_CHECK(cu_seqlens_t.dtype() == torch::kLong, "cu_seqlens must be int64");
        N_val = cu_seqlens_t.numel() - 1;
        TORCH_CHECK(N_val > 0, "cu_seqlens must have at least 2 elements");
```

[csrc/flash_kda.cpp:192-213](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L192-L213) —— `DISPATCH_STATE` 宏：7 种 `(has_state_in, has_state_out, state_fp32)` 组合各启动一个模板实例，varlen/batched 再各复制一份（共 14 个实例）。本讲只需要读懂分支的结构——第一个分支就是 stateless：

```cpp
    #define DISPATCH_STATE(VL) \
        if (!has_state_in && !has_state_out) { \
            LAUNCH(false, false, false, VL); \
        } else if (has_state_in && has_state_out && state_fp32) { \
            LAUNCH(true, true, true, VL); \
        } ...
```

最后看 varlen 的标准范例。[tests/test_fwd.py:265-297](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test_fwd.py#L265-L297) —— `test_fwd_varlen`：6 条长度互不相同（且多数不是 16 的倍数）的序列拼成总长 8192 的输入；`cu_seqlens` 用 `torch.long` 构造且首元素为 0；注意与 batched 版对照的三处变化——`q` 的 B 维为 1、`initial_state` 第 0 维是 N=6、调用时多传 `cu_seqlens`：

```python
    seq_lens = [1300, 547, 2048, 963, 271, 3063]
    T_total = sum(seq_lens)
    N = len(seq_lens)
    cu_seqlens = torch.tensor([0] + list(torch.cumsum(...)), dtype=torch.long, device='cuda')

    q = ...  # 形状 (1, T_total, H, D)
    initial_state = torch.arange(N * H * D * D, ...).reshape(N, H, D, D).to(torch.bfloat16)  # 第 0 维是 N！

    flash_kda.fwd(q, k, v, g, beta, scale, out_kernel,
                  A_log=A_log, dt_bias=dt_bias, lower_bound=LOWER_BOUND,
                  initial_state=initial_state.clone(), final_state=final_state_kernel,
                  cu_seqlens=cu_seqlens)
```

#### 4.3.4 代码实践：batched 与 varlen 的等价性实验

**实践目标**：用同一段输入验证「batched 的每个 batch 元素」与「varlen 的每条序列」在数学上是同一种独立递推——分界线对齐时两种模式应当给出逐位一致的结果。

**操作步骤**（示例代码，实践脚本）：

```python
# mode_equiv.py —— B=2 等长 batch vs varlen 两段拼接，结果应逐位一致
import torch, math, flash_kda

torch.manual_seed(0)
H, D, T_seg = 2, 128, 64                      # 每段 64（16 的倍数，两段共 128）
dev = 'cuda'

# 生成一份 [2, T_seg, H, D]，再 reshape 成 [1, 2*T_seg, H, D] —— 两份数据完全相同
def make_inputs(B, T):
    return dict(
        q=torch.randn(B, T, H, D, dtype=torch.bfloat16, device=dev),
        k=torch.randn(B, T, H, D, dtype=torch.bfloat16, device=dev),
        v=torch.randn(B, T, H, D, dtype=torch.bfloat16, device=dev),
        g=torch.randn(B, T, H, D, dtype=torch.bfloat16, device=dev),
        beta=torch.randn(B, T, H, dtype=torch.bfloat16, device=dev),
    )

i_b = make_inputs(2, T_seg)                            # batched：B=2, T=64
i_v = {k: t.reshape(1, 2 * T_seg, *t.shape[2:]) for k, t in i_b.items()}  # varlen：B=1, T=128

A_log = torch.rand(H, dtype=torch.float32, device=dev)
dt_bias = torch.rand(H, D, dtype=torch.float32, device=dev)
kw = dict(A_log=A_log, dt_bias=dt_bias, lower_bound=-5.0)
scale = 1.0 / math.sqrt(D)

# (a) batched：B=2，每 batch 独立从零状态递推
out_b = torch.zeros_like(i_b['q'])
flash_kda.fwd(**i_b, scale=scale, out=out_b, **kw)

# (b) varlen：cu_seqlens 在 token 64 处切开，两段各自独立从零状态递推
cu = torch.tensor([0, T_seg, 2 * T_seg], dtype=torch.long, device=dev)
out_v = torch.zeros_like(i_v['q'])
flash_kda.fwd(**i_v, scale=scale, out=out_v, cu_seqlens=cu, **kw)
torch.cuda.synchronize()

same = torch.equal(out_b, out_v.reshape_as(out_b))
print("batched 与 varlen 输出逐位一致:", same)
if not same:
    diff = (out_b.float() - out_v.float().reshape_as(out_b)).abs()
    print("max diff:", diff.max().item(), " 非零位置数:", (diff > 0).sum().item())
```

**需要观察的现象**：`torch.equal` 为 True。直觉解释：batched 模式下 batch 0 与 batch 1 各自独立递推；varlen 模式下段 0（token 0~63）与段 1（token 64~127）也各自独立递推；分界线恰好在 tile 边界（64 是 16 的倍数）时，两种模式的每条序列看到完全相同的输入与完全相同的 chunk 划分，计算路径一致。

**预期结果**：逐位一致（待本地验证）。若你把 `T_seg` 改成非 16 倍数（如 60）再实验，两种模式的 chunk 划分将不再对齐，差异会被 `max diff` 打印出来——这也是理解「CHUNK=16 划分如何影响数值」的第一手材料。

#### 4.3.5 小练习与答案

**练习 1**：varlen 模式下 `cu_seqlens=[0, 100, 250, 250]`（第三条序列长 0）合法吗？kernel 会怎么处理？

答案：校验层面合法：`cu_seqlens` 满足 int64、1D、N+1 个元素（N=3）的所有检查（[csrc/flash_kda.cpp:149-157](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L149-L157)）。长度为 0 的序列没有 tile，K2 对它的递推是「空转」——初始状态直接成为最终状态。具体边界行为（如 0 长序列的 final_state 是否等于 initial_state）建议用 4.3.4 的脚本改造验证，待本地验证。

**练习 2**：为什么 `initial_state` 用 bf16、`final_state` 用 fp32 会直接报错，而不是「各自按各自精度处理」？

答案：见 [csrc/flash_kda.cpp:76-79](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L76-L79) 的显式 `TORCH_CHECK`。设计原因：模板分发只按「一个 state_fp32 布尔值」实例化（7 分支的 `DISPATCH_STATE`），若允许进出 dtype 不同，组合数翻倍且必须定义量化/反量化的确切位置；强制一致让「fp32 模式 = 进出口都是 fp32」的语义保持简单。

**练习 3**：推理服务里逐段解码：第一段算完想缓存状态供第二段用，两段之间怎么传？

答案：第一段调用时同时给 `initial_state=None`（或历史状态）与 `final_state=state_cache`（bf16 或 fp32）；第二段调用时把 `state_cache` 作为 `initial_state` 传入。由于 KDA 的递推只依赖 \( S_{t-1} \) 与当前 token，这种「状态接力」在数学上与一次算完整段等价（精度上受状态 dtype 影响，fp32 缓存的偏差更小——u3-l6 会精读这两条路径）。

## 5. 综合实践

现在把三个模块串起来，完成本讲的主任务 **mini_run.py**：在真实规模（B=1、T=4096、H=32、D=128）下，分别以（a）无状态、（b）bf16 初始/最终状态两种模式调用 `flash_kda.fwd`，用 `torch.cuda.Event` 计时，并打印 `out` 与 `final_state` 的统计信息。

```python
# mini_run.py —— 本讲综合实践（示例代码）
import torch, math, flash_kda
import torch.nn.functional as F

torch.manual_seed(0)
B, T, H, D = 1, 4096, 32, 128
dev = 'cuda'
LOWER_BOUND = -5.0

# ---- 输入构造（对齐 tests/test_fwd.py 的惯例）----
q = F.normalize(torch.randn(B, T, H, D, device=dev), p=2, dim=-1).to(torch.bfloat16)
k = F.normalize(torch.randn(B, T, H, D, device=dev), p=2, dim=-1).to(torch.bfloat16)
v = torch.randn(B, T, H, D, dtype=torch.bfloat16, device=dev)
g = torch.randn(B, T, H, D, dtype=torch.bfloat16, device=dev)
beta = torch.randn(B, T, H, dtype=torch.bfloat16, device=dev)
A_log = torch.rand(H, dtype=torch.float32, device=dev)
dt_bias = torch.rand(H, D, dtype=torch.float32, device=dev)
scale = 1.0 / math.sqrt(D)

def bench(fn, warmup=10, iters=50):
    for _ in range(warmup):
        fn()
    torch.cuda.synchronize()
    s = torch.cuda.Event(enable_timing=True); e = torch.cuda.Event(enable_timing=True)
    s.record()
    for _ in range(iters):
        fn()
    e.record(); torch.cuda.synchronize()
    return s.elapsed_time(e) / iters          # 平均每次毫秒数

def stats(name, t):
    print(f"  {name}: mean={t.float().mean():+.6f}  absmax={t.float().abs().max():.6f}")

# ---- (a) stateless：从零开始，不产出状态 ----
out_a = torch.zeros_like(v)
t_a = bench(lambda: flash_kda.fwd(q, k, v, g, beta, scale, out_a,
                                  A_log=A_log, dt_bias=dt_bias, lower_bound=LOWER_BOUND))
print(f"(a) stateless   {t_a:.3f} ms/call"); stats("out", out_a)

# ---- (b) bf16 状态：随机初始状态 + 最终状态输出 ----
h0 = (0.01 * torch.randn(B, H, D, D, device=dev)).to(torch.bfloat16)   # [B, H, V, K]
ht = torch.zeros_like(h0)
out_b = torch.zeros_like(v)
t_b = bench(lambda: flash_kda.fwd(q, k, v, g, beta, scale, out_b,
                                  A_log=A_log, dt_bias=dt_bias, lower_bound=LOWER_BOUND,
                                  initial_state=h0, final_state=ht))
print(f"(b) bf16 state  {t_b:.3f} ms/call"); stats("out", out_b); stats("final_state", ht)

# ---- 观察点 ----
diff = (out_a.float() - out_b.float()).abs()          # 初始状态带来的输出差异
per_tok = diff.amax(dim=(0, 2, 3))
print("初始状态影响：前 16 token 的最大差异 %.6f，后 16 token %.6f"
      % (per_tok[:16].max(), per_tok[-16:].max()))
```

**操作步骤**：

1. 确认环境满足要求（u1-l1：SM90+、CUDA 12.9+、PyTorch 2.4+）且已按 u1-l3 安装 FlashKDA。
2. 运行 `python mini_run.py`。
3. 记录两种模式的耗时、`out` / `final_state` 的 mean 与 absmax、以及首尾 token 的差异对比。

**需要观察的现象与预期结果**（待本地验证）：

1. 两种模式耗时接近（带状态只多一次 128×128 状态的 TMA 载入/写出，占总耗时比例很小）；计时包含了包装层的 workspace 分配（约 108 MiB，见 4.1.4），kernel 纯耗时的测法在 u3-l10。
2. `out` 的 absmax 是个位数级别（q/k 已 L2 归一化、乘 `scale=1/√128` 后的点积量级）。
3. `final_state` 的 absmax 明显大于 `h0`（0.01 量级的初始状态经过 4096 个 token 的持续写入）。
4. 「前 16 token 差异 > 后 16 token 差异」：初始状态的影响被门控按 \( e^{\sum g} \) 逐 token 衰减，越靠后的 token 受 \( S_0 \) 影响越小。
5. **附加实验**：把 `h0` 换成全零（`torch.zeros`），此时 (b) 与 (a) 数学上等价，`out_b` 应与 `out_a` **逐位一致**——等价于 4.3.4 的对照逻辑。

## 6. 本讲小结

- `flash_kda.fwd` 的 Python 层只做三件事：推导 `(T_total, H, N)`、自动分配 workspace、转发给 C++；调用者完全不必感知 workspace。
- workspace 尺寸公式：\( H \times (\lceil T_{total}/16 \rceil + N) \times 13824 + \) 128 字节对齐的 tile_prefix 尾部；`N` 是「独立递推的序列条数」（batched=B，varlen=cu_seqlens 段数），量级可达数百 MiB。
- 输入规格的记忆骨架：`q/k/v/g [B,T,H,128]` + `beta [B,T,H]` 全 bf16 且 CUDA、contiguous；`A_log [H]`、`dt_bias [H,128]` fp32；`lower_bound ∈ [-5,0]`；`out` 是预分配的原地输出缓冲；**D=128 硬约束**；`g`/`beta` 传**激活前 logits**。
- `initial_state`/`final_state` 形状为 `[B,H,V,K]`（batched）或 `[N,H,V,K]`（varlen），第 2 轴是 V、第 3 轴是 K；两者都给时 dtype 必须一致（bf16 或 fp32），共 7 种合法组合、由 C++ 模板分发承接。
- varlen 模式由 `cu_seqlens`（int64、`[N+1]`、CUDA）触发，此时 B 必须为 1、T 是总长度、每条序列独立递推；分界线对齐 tile 边界时与 batched 模式逐位等价。
- kernel 内部会做 q/k 的 L2 归一化，调用者传原始值即可；测试脚本里的 `F.normalize` 是 bit-exact 对拍的惯例而非调用要求。

## 7. 下一步学习建议

本讲之后，你已经能熟练「用」FlashKDA。入门单元到此完成，第二单元（u2）将沿 u1-l4 的调用链**自顶向下进入源码**：

- **u2-l1（下一讲）**：精读 `tests/torch_ref.py`——CHUNK=16 分块下 KDA 的矩阵形式（k_decayed/k_inv/k_restored、`(I+L)^{-1}` 等），把 u1-l2 的逐 token 递推改写成 chunk 矩阵语言。这是读懂一切 kernel 代码的先修课。
- **u2-l2**：本讲 4.2.3 只摘了校验链的片段，u2-l2 将完整精读 `csrc/flash_kda.cpp` 的全部 `TORCH_CHECK` 与布局预处理（beta 转置、`gate_scale` 预乘 log2(e)），并系统性地做「非法输入 → 报错」对照实验。
- 若你想先巩固本讲内容，推荐重读 [tests/test_fwd.py:222-312](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test_fwd.py#L222-L312) 的两个 exact-match 测试，并对照本讲规格表逐参数标注「它是谁、为什么是这个形状」。
