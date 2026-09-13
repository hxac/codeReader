# 启动配置：TMA 描述符、gmem 布局与 workspace 切分

> 学习阶段：intermediate ｜ 依赖：u2-l2（pybind 入口与校验链）、u2-l4（CuTe 布局基础）

## 1. 本讲目标

上一讲（u2-l4）我们学会了「读」CuTe 布局；本讲要学会「用」：`launch_fwd` 这个 host 侧函数如何在启动 kernel 之前，把裸指针变成 TMA 描述符、把一整块 workspace 切成六个数组、并为两个 kernel 配置各自的 grid。读完本讲你应当能：

1. 解释 q/k/v/g/out 五个张量为什么能共用同一个 `gmem_layout`，而 workspace 六个数组却各自需要独立的 TMA 描述符。
2. 给定 \((H, \text{total\_tiles})\)，手工算出 workspace 中六段数组（k_decayed / q_decayed / k_restored / g_total / INV / Mqk）及尾部 tile_prefix 的字节偏移，并与 `get_workspace_size` 的返回值对上账。
3. 说出 K1 的 grid \((\text{total\_tiles}, H)\) 与 K2 的 grid \((N, H)\) 各对应哪条并行轴、为什么必须不同；以及 `BLOCK_LEVEL_K1/K2`、`cudaFuncSetAttribute` 这两个启动细节的作用。

## 2. 前置知识

**TMA 与 TMA 描述符（TensorMap）**。SM90（Hopper）引入了 TMA（Tensor Memory Accelerator）异步搬运引擎：一条设备端指令就能在全局内存与共享内存之间搬一整块 tensor tile，硬件自动处理边界、地址计算和 swizzle。host 端需要先编码一张约 128 字节的「搬运说明书」——TensorMap，里面记录全局内存盒子的形状、步长、基地址、swizzle 模式。kernel 启动时把这张说明书**按值放进 kernel 参数空间**（CuTe 用 `CUTE_GRID_CONSTANT`，即 `const __grid_constant__` 修饰），设备端凭它发起搬运。

**`make_tma_copy` 的三要素**。本项目构造描述符只靠一个函数模板：

```cpp
auto tma = make_tma_copy(SM90_TMA_LOAD{},  m_g,  TMAQKLayout{});
//                              ①             ②          ③
```

- ① **copy atom** 决定方向：`SM90_TMA_LOAD` 是 gmem→smem，`SM90_TMA_STORE` 是 smem→gmem；
- ② **gmem tensor**（裸指针 + 布局包装成的 `Tensor`）决定全局内存一侧的盒子形状与基地址；
- ③ **smem layout** 决定数据落到共享内存后的排布（可以带 swizzle，见 u2-l4）。

注意 `make_tensor` 只是在 host 侧把「指针 + 布局」包装成元数据对象，**不搬运任何数据**。

**动态共享内存的 opt-in**。kernel 用 `extern __shared__` 声明动态共享内存时，默认上限是 48 KB；超过就必须先用 `cudaFuncSetAttribute(kernel, cudaFuncAttributeMaxDynamicSharedMemorySize, bytes)` 显式申请（SM90 每 block 上限约 227 KB）。

**两个 total_tiles**。u2-l2 提过 workspace 尺寸用「上界」公式分配；而 launch 时 host 侧还会再算一次 `total_tiles`——batched 模式下是**精确值**、varlen 模式下仍是上界。本讲 4.2 会把这两个口径彻底分开。

## 3. 本讲源码地图

| 文件 | 本讲关注点 |
| --- | --- |
| [csrc/smxx/fwd_launch.cu](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu) | 主角：`launch_fwd` 全函数——gmem 布局、workspace 切分、22 个 TMA 描述符、两个 grid 与显式实例化表 |
| [csrc/smxx/utils.cuh](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh) | `WorkspaceSizes`（每 tile 六段的字节数与 128 字节对齐断言）、`BLOCK_LEVEL_K1/K2` 宏 |
| [csrc/flash_kda.cpp](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp) | 对照：`get_workspace_size` 分配公式、host 侧 `total_tiles` 两个口径、reshape/beta 转置预处理 |
| [flash_kda/__init__.py](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/flash_kda/__init__.py) | Python 包装层如何拿到 N 并分配 workspace |
| [csrc/smxx/fwd_kernel1.cuh](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh) | K1Layouts 的 TMA 布局别名、`CUTE_GRID_CONSTANT` 参数、grid 映射与 early-return、tile prefix kernel |
| [csrc/smxx/fwd_kernel2.cuh](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh) | K2Layouts 的 TMA 布局别名（含 fp32 状态布局）、K2 的 grid 映射 |

## 4. 核心概念与源码讲解

### 4.1 gmem 布局与 tensor 构造

#### 4.1.1 概念说明

用户传入的 q/k/v/g/out 是 `[B, T, H, D]` 的连续 bf16 张量。连续意味着内存序是「token 主序」：任意元素 \((b,t,h,d)\) 的字节地址为

\[
\text{addr} = \text{base} + \big(((b \cdot T + t)\cdot H + h)\cdot D + d\big)\times 2 .
\]

u2-l2 讲过 pybind 层把它们零拷贝 reshape 成 `[B*T, H, D]`。launch 层要做的，是把这种内存序翻译成 CuTe 布局：以 **(H, T, D) 的坐标顺序**（head 是第 0 模式）写出来就是 `(H, T, D) : (D, D*H, 1)`——head 方向走 D 个元素就到下一个 head，token 方向走 `D*H` 个元素，通道方向连续。**布局的模式顺序是访问坐标的顺序，不是内存序本身**；只要 (h,t,d) 经布局映射出的偏移与上式一致，就是同一份内存的等价描述。

为什么把 H 放第 0 模式？因为两个 kernel 的并行粒度都是「(tile, head)」：设备端用 `layout(head_idx, token, d)` 一跳就到目标 tile 的起点，然后以 `(1, CHUNK, D)` 的盒子发起 TMA——head 模式的盒子宽度为 1，保证一次搬运绝不跨 head。

#### 4.1.2 核心流程

1. pybind 层：`[B,T,H,D]` reshape 成 `[B*T,H,D]`（零拷贝）；beta 转置 `[T,H]→[H,T]` 后 contiguous（唯一一次真实拷贝）；推算 `N` 与两个口径的 `total_tiles`。
2. launch 层：构造三个布局——五合一的 `gmem_layout (H,T_total,D)`、beta 的一维布局 `(H*T_total,)`、state 的 `(N*H, D, D)`。
3. 用 `make_tensor(make_gmem_ptr(ptr), layout)` 把裸指针包装成 Tensor（纯元数据）。
4. 这些 Tensor 之后只干一件事：作为 `make_tma_copy` 的第二参数编码 TMA 描述符。

#### 4.1.3 源码精读

pybind 层的 reshape 与 beta 转置（u2-l2 已细讲，这里是它在 launch 侧的落点）：[csrc/flash_kda.cpp:L112-L118](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L112-L118) 把六个张量零拷贝展平；[csrc/flash_kda.cpp:L130-L132](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L130-L132) 完成转置，注释点明动机——1D TMA 不受 T 方向对齐约束。

launch 层构造三个 gmem 布局并包装前五个 Tensor：

```cpp
auto gmem_layout = make_layout(make_shape(H, T_total, D), make_stride(D, D * H, 1));
auto beta_gmem_layout = make_layout(make_shape(H * T_total));          // 1D，拉平的 [H,T]
auto state_gmem_layout = make_layout(make_shape(N * H, D, D), LayoutRight{});

Tensor m_q   = make_tensor(make_gmem_ptr(q_ptr), gmem_layout);
Tensor m_k   = make_tensor(make_gmem_ptr(k_ptr), gmem_layout);
Tensor m_v   = make_tensor(make_gmem_ptr(v_ptr), gmem_layout);
Tensor m_out = make_tensor(make_gmem_ptr(out_ptr), gmem_layout);
Tensor m_beta = make_tensor(make_gmem_ptr<BF16>(beta_ptr), beta_gmem_layout);
```

见 [csrc/smxx/fwd_launch.cu:L49-L59](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L49-L59)：一个 `gmem_layout` 对象被 q/k/v/out 四个 Tensor 复用——因为它们形状、步长完全相同，只有基地址不同。第五个同型张量 g 稍后在 K1 描述符区才构造：[csrc/smxx/fwd_launch.cu:L91-L92](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L91-L92)（`m_g` 同样用 `gmem_layout`）。

state 布局把 (N,H) 两个模式合并成 `N*H` 一个模式（[csrc/smxx/fwd_launch.cu:L53](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L53)），好处在设备端直接可见：K2 用 `g_init.layout()(seq_idx * H + head_idx, 0, 0)` 一个整数索引就定位到某个 (序列， head) 的状态块（见 [csrc/smxx/fwd_kernel2.cuh:L251-L254](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L251-L254)）。

布局在设备端的用法（K1 内部，帮助理解布局模式顺序的含义）：

```cpp
auto qk_off = g_q.layout()(head_idx, int(bos) + local_t * CHUNK, 0);
auto tile_shape_3d = make_shape(Int<1>{}, Int<CHUNK>{}, Int<D>{});
```

出自 [csrc/smxx/fwd_kernel1.cuh:L216-L219](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L216-L219)：以 (head, token, 0) 求偏移，TMA 盒子 `(1, 16, 128)` 只在本 head 内取 16 个 token。beta 侧同理用一维坐标 `head_idx * T_total + token` 寻址（[csrc/smxx/fwd_kernel1.cuh:L222-L225](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L222-L225)，注意 `& ~7` 的 8 元素对齐，u3-l3 会展开）。

#### 4.1.4 代码实践

**目标**：用 PyTorch 亲手验证「reshape 零拷贝 + (H,T,D):(D,D*H,1) 内存序」这两件事。

```python
# mem_layout_probe.py（示例代码）
import torch

B, T, H, D = 1, 5, 3, 128
dev = "cuda" if torch.cuda.is_available() else "cpu"
q = torch.randn(B, T, H, D, dtype=torch.float32, device=dev)  # cpu 上也成立

q3 = q.reshape(T, H, D)                      # 模拟 pybind 层的 [B*T,H,D]
print("q  stride:", q.stride())              # 预期 (T*H*D, H*D, D, 1)
print("q3 stride:", q3.stride())             # 预期 (H*D, D, 1)

# 1) reshape 是否零拷贝？
print("same data_ptr:", q.data_ptr() == q3.data_ptr())        # 预期 True

# 2) 地址公式：addr(t,h) = base + (t*H + h)*D 个元素
t, h = 3, 2
expect_off = (t * H + h) * D
got_off = (q3[t, h, 0].data_ptr() - q.data_ptr()) // q.element_size()
print("offset match:", expect_off == got_off)                 # 预期 True
# 再对照 CuTe 语义：H 方向相邻 head 差 D 个元素，T 方向差 H*D 个元素
print("H-step:", (q3[0,1,0].data_ptr()-q3[0,0,0].data_ptr())//4,
      "T-step:", (q3[1,0,0].data_ptr()-q3[0,0,0].data_ptr())//4)  # 预期 128 与 384
```

**观察现象与预期结果**：三个判断全部为 True，两个 step 分别是 `D=128` 与 `H*D=384`——这正是 `make_stride(D, D*H, 1)` 的元素步长。若把 `q` 换成非连续张量（例如 `q.transpose(1,2)` 的产物），第一个判断就会失败，这也解释了 pybind 层为何强制 contiguous（待本地验证，CPU 上即可完成）。

#### 4.1.5 小练习与答案

1. **练习**：如果用户传入非连续的 q，会在哪一步、以什么报错失败？为什么 launch 层不能像普通 PyTorch 算子那样兼容任意 stride？
   **答案**：在 [csrc/flash_kda.cpp:L46-L47](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L46-L47) 的 `TORCH_CHECK(... "all tensors must be contiguous")` 处报错。因为 launch 层的 `gmem_layout` 把步长**硬编码**为 `(D, D*H, 1)`、且 TMA 描述符要求内维连续；支持任意 stride 就要为每种步长组合编码不同的 TensorMap，代价远大于让调用方先 contiguous。
2. **练习**：beta 为什么不走 `(H, T_total)` 的二维 TMA，而要先转置再拉平成一维？
   **答案**：varlen 下序列起点是任意 token，二维盒子在 T 方向按 16 行对齐会取到相邻序列的数据（或越界）；1D 拷贝按字节流搬 `H*T` 数组，配合 `& ~7` 对齐和 smem 内偏移补偿（[csrc/smxx/fwd_kernel1.cuh:L222-L225](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L222-L225)）就没有 T 对齐约束。这正是 pybind 层花一次转置拷贝买来的自由（u2-l2）。
3. **练习**：`state_gmem_layout` 为什么是 3 维 `(N*H, D, D)` 而不是 4 维 `(N, H, D, D)`？
   **答案**：TMA 描述符最多支持 5 维，但更实际的原因是合并 (N,H) 后设备端定位一块状态只需一个整数下标 `seq_idx * H + head_idx`（[csrc/smxx/fwd_kernel2.cuh:L252](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L252)），描述符维度越低、编码与寻址越简单。

### 4.2 workspace 六段切分

#### 4.2.1 概念说明

workspace 是 K1→K2 的传送带（u1-l4）：K1 每个 (tile, head) 往里写六个中间量，K2 再按同一套地址读。它是一块**一维字节缓冲**（Python 侧 `torch.empty(..., dtype=torch.uint8)`，见 [flash_kda/__init__.py:L38](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/flash_kda/__init__.py#L38)），launch 层负责按 `WorkspaceSizes` 给出的每 tile 字节数把它切成六段数组 + 尾部 tile_prefix。

每 tile 的六段内容与大小（CHUNK=16、D=128）：

| 段 | 含义 | 形状/tile | dtype | 字节 |
| --- | --- | --- | --- | --- |
| k_decayed | 衰减后的 k | (16, 128) | bf16 | \(16\times128\times2 = 4096\) |
| q_decayed | 衰减后的 q | (16, 128) | bf16 | 4096 |
| k_restored | 还原后的 k | (16, 128) | bf16 | 4096 |
| g_total | 整块衰减因子 exp(g_total) | (128,) | fp32 | \(128\times4 = 512\) |
| INV | (I+L)⁻¹ | (16, 16) | bf16 | \(16\times16\times2 = 512\) |
| Mqk | 块内查询矩阵 | (16, 16) | bf16 | 512 |
| **合计 kPerTile** | | | | **13824** |

六段大小全部是 128 的倍数（`static_assert` 在编译期固化），于是**任意前缀和也 128 对齐**——每段的起始指针天然满足 TMA 描述符对全局地址的对齐要求，且 128 字节恰好是一条缓存行。workspace 基地址由 PyTorch CUDA 缓存分配器保证粗粒度对齐（块粒度 512 字节），因此整条缓冲的所有段都是 128 字节对齐的。

还有个容易踩的坑：**存在两个不同口径的 total_tiles**。

- **分配口径**（`get_workspace_size`）：上界 \(\lceil T_{total}/16\rceil + N\)。加 \(N\) 是因为 varlen 下每条序列的不完整尾 tile 各占一格：\(\sum_i \lceil \text{len}_i/16\rceil \le \lceil T_{total}/16\rceil + N\)（由 \(\lceil a\rceil+\lceil b\rceil \le \lceil a+b\rceil+1\) 归纳可得，且上式右端还富余 1）。
- **启动口径**（`fwd` 内再算一次）：batched 下是精确值 \(N\times\lceil T_{seq}/16\rceil\)，varlen 下仍用上界。

launch 层切分用的是**启动口径**。batched 模式下启动口径严格小于等于分配口径，workspace 尾部留有一点松弛——分配公式只看 \((T_{total}, H, N)\) 三个数、不读设备上的 cu_seqlens，这是刻意的设计取舍。

#### 4.2.2 核心流程

1. `n_ht = H × total_tiles`（启动口径）：六个数组都是长度为 n_ht 的「tile 槽位」数组。
2. 六个指针按**前缀和**依次偏移：

\[
\text{ptr}_i = \text{ws} + n_{ht}\cdot\sum_{j<i} s_j,\qquad s = (4096, 4096, 4096, 512, 512, 512)
\]

3. tile_prefix 指针放在最末：`ws + n_ht * kPerTile`，即六段数组之后。
4. 给六段各建 gmem 布局并包装 Tensor：三个 (16,128) 段共用 `(n_ht, CHUNK, D)` 行主布局；g_total 是 `(n_ht, D)` fp32；INV/Mqk 是 `(n_ht, CHUNK, CHUNK)`。

#### 4.2.3 源码精读

每段字节与对齐断言的定义：[csrc/smxx/utils.cuh:L63-L77](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L63-L77)

```cpp
template <int CHUNK, int D>
struct WorkspaceSizes {
    static_assert(CHUNK * D * 2 % 128 == 0);
    static_assert(D * 4 % 128 == 0);
    static_assert(CHUNK * CHUNK * 2 % 128 == 0);
    static constexpr int kKDecayed  = CHUNK * D * 2;   // 4096
    ...
    static constexpr int64_t kPerTile = kKDecayed + kQDecayed + kKRestored
                                      + kGTotal + kINV + kMqk;   // 13824
};
```

launch 层的前缀和切指针与布局（注意 `WS` 是 `WorkspaceSizes<CHUNK, D>` 的别名，[csrc/smxx/fwd_launch.cu:L35](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L35)）：[csrc/smxx/fwd_launch.cu:L61-L77](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L61-L77)

```cpp
int64_t n_ht = int64_t(H) * total_tiles;
char* ws = reinterpret_cast<char*>(workspace_ptr);
BF16*  ws_kd  = reinterpret_cast<BF16*>(ws);
BF16*  ws_qd  = reinterpret_cast<BF16*>(ws + n_ht * WS::kKDecayed);
BF16*  ws_kr  = reinterpret_cast<BF16*>(ws + n_ht * (WS::kKDecayed + WS::kQDecayed));
float* ws_gt  = reinterpret_cast<float*>(ws + n_ht * (WS::kKDecayed + WS::kQDecayed
                                                     + WS::kKRestored));
BF16*  ws_inv = ...   // 再累加 kGTotal
BF16*  ws_mqk = ...   // 再累加 kINV
int* ws_tile_prefix = reinterpret_cast<int*>(ws + n_ht * WS::kPerTile);
```

随后 [csrc/smxx/fwd_launch.cu:L73-L84](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L73-L84) 建布局并包装成六个 Tensor：`ws_kd/qd/kr` 共用 `(n_ht, CHUNK, D)`，`ws_gt` 是 `(n_ht, D)`，`ws_inv/ws_mqk` 共用 `(n_ht, CHUNK, CHUNK)`。gmem 侧一律自然行主序——swizzle 只发生在 smem 一侧（由 TMA 布局描述，见 4.3）。

分配口径（对拍基准）：[csrc/flash_kda.cpp:L5-L26](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L5-L26)。L14 是上界公式，L20 是每 tile 字节 `3*16*128*2 + 128*4 + 2*16*16*2 = 13824`，L23 把 `(N+1)` 个 int32 的 tile_prefix 向上取整到 128 字节，L25 汇总。启动口径：[csrc/flash_kda.cpp:L176-L181](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L176-L181)，注释明确写着 `upper bound for varlen` 与 `exact for batched`。

tile_prefix 是长度 N+1 的 int32 前缀和数组（`tile_prefix[i]` = 前 i 条序列的 tile 数之和），只在 varlen 模式下由前置小 kernel 写入（见 4.3.3）、被 K1 的二分查找消费（u2-l6 精读）；batched 模式分配了但不用。

#### 4.2.4 代码实践

**目标**：不写代码，手算一个小例子的全部偏移，再用一行 Python 验证。

设 H=2、启动口径 total_tiles=5（例如 varlen 下三条序列 tile 数 1+2+2=5 的情形），则 \(n_{ht}=10\)：

| 段 | 起始偏移 | 大小（= n_ht×段字节） |
| --- | --- | --- |
| k_decayed | 0 | 10×4096 = 40960 |
| q_decayed | 40960 | 40960 |
| k_restored | 81920 | 40960 |
| g_total | 122880 | 10×512 = 5120 |
| INV | 128000 | 5120 |
| Mqk | 133120 | 5120（结束于 138240） |
| tile_prefix | 138240 | 向上取整到 128 的 \((N+1)\times4\) |

验证：`python -c "print([10*s for s in (4096,4096,4096,512,512,512)])"` 应输出上述大小；每段起始偏移模 128 都为 0。**预期结果**：六段加 tile_prefix 的起点构成等差可复现的表，且任意段起始偏移 % 128 == 0（这就是三个 `static_assert` 的意义）。

#### 4.2.5 小练习与答案

1. **练习**：batched 模式 B=1、T_seq=4096、H=32，求分配大小与启动占用。
   **答案**：分配口径 total_tiles = \(\lceil 4096/16\rceil+1 = 257\)，分配 = \(32\times257\times13824 + 128 = 113{,}688{,}704\) 字节 ≈ 108.4 MiB；启动口径 = 256（精确），实际占用 \(32\times256\times13824+128 = 113{,}246{,}336\) 字节。差值恰好是 \(H\times13824\)（一个 tile 槽的富余）。
2. **练习**：tile_prefix 为什么放在缓冲区**末尾**而不是开头？
   **答案**：它的字节数随 N 变化（\(\lceil (N{+}1)\times4/128\rceil\times128\)）。放在末尾，六段主数组的偏移只依赖 \(n_{ht}\) 与常量段大小，结构规整；若放在开头，后面所有段的基地址都要叠加一个随 N 变化的前缀，还得为对齐补 padding。这也和「六段是 K1/K2 的主数据通路、tile_prefix 只是 varlen 索引附属品」的地位一致。
3. **练习**：为什么 workspace 校验只查 device/contiguous 而不查大小（u2-l2）？大小一致性由什么机制保证？
   **答案**：Python 包装层用**同一个公式**（`get_workspace_size`）分配（[flash_kda/__init__.py:L38](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/flash_kda/__init__.py#L38)），host 侧无需在 C++ 里重复对账；C++ 只做廉价检查。单源真理（公式只有一份 C++ 实现，Python 直接调用它）保证了分配 ≥ 启动占用。

### 4.3 TMA 描述符家族与两个 grid

#### 4.3.1 概念说明

**为什么 22 个描述符、不能更少？** 一个 TensorMap 绑定「基地址 + gmem 盒子 + smem 排布」三件事：

- q/k/v/g/out 共用一个 `gmem_layout` 是因为它们形状步长全同——但**描述符仍然各建一个**，因为基地址不同，且 smem 侧布局也可以不同：q/k/g 在 K1 里被线程逐元素消费，用自然的 `TMAQKLayout`；v/out 参与 K2 的 MMA，用 swizzle 的 `TMAVOLayout`。
- workspace 六段必须各建描述符：形状与 dtype 有三种（(16,128) bf16、(128,) fp32、(16,16) bf16），基地址六个，smem 布局三种（TMAVOLayout / TMAGTotalSmemLayout / TMALMLayout）。
- **位一致契约**：K1 的 6 个 `SM90_TMA_STORE` 与 K2 的 6 个 `SM90_TMA_LOAD` 用**同一批 `m_ws_*` Tensor + 同名同型的 smem 布局**（K1Layouts 与 K2Layouts 中 `TMAVOLayout`/`TMALMLayout`/`TMAGTotalSmemLayout` 的构造完全相同，见 [csrc/smxx/fwd_kernel1.cuh:L29-L41](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L29-L41) 与 [csrc/smxx/fwd_kernel2.cuh:L42-L57](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L42-L57)）——gmem 侧自然序、smem 侧同款 swizzle，保证 K1 写下的比特就是 K2 读到的比特（u2-l4 的结论在描述符层面的落地）。

**state 描述符的条件构造**：状态有 bf16/fp32 两类 smem 布局（不同的 C++ 类型），必须 `if constexpr (StateFP32)` 编译期二选一；无状态输入/输出时用 `out_ptr` 当**哑指针**保证描述符仍能编码出一个合法对象，kernel 内部由 `if constexpr` 守卫、永远不会真正发起这次搬运。

**两个 grid 对应两条并行轴**：K1 的块内计算只依赖本 tile 的输入 → (tile, head) 全并行；K2 的第 t 个 tile 依赖第 t−1 个 tile 的状态 → 序列内必须串行，只能按 (序列, head) 并行。

#### 4.3.2 核心流程

```
launch_fwd
 ├─ 构造 gmem 布局/Tensor（4.1）
 ├─ 切 workspace 六段 + tile_prefix（4.2）
 ├─ K1 描述符：5 个 LOAD（q,k,beta,g,dt_bias）+ 6 个 STORE（六段）
 ├─ K2 描述符：8 个 LOAD（v,beta2,六段）+ 1 个 STORE（out）
 ├─ make_state_tma()：if constexpr(StateFP32) 选 fp32/bf16 布局；
 │                    无 in/out 状态时用 out_ptr 哑指针
 ├─ [varlen] 先启动 _flash_kda_build_tile_prefix<<<1,32>>>
 ├─ K1: cudaFuncSetAttribute → grid(total_tiles, H) × 256 线程
 └─ K2: cudaFuncSetAttribute → grid(N, H) × 192 线程
```

#### 4.3.3 源码精读

**描述符清单**（[csrc/smxx/fwd_launch.cu:L86-L116](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L86-L116)）：

| 变量 | atom | gmem Tensor | smem 布局 | 行 |
| --- | --- | --- | --- | --- |
| tma_load_q / _k | LOAD | m_q / m_k | TMAQKLayout（自然序） | [L87-L88](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L87-L88) |
| tma_load_beta | LOAD | m_beta（1D） | TMABetaSmemLayout（1D） | [L89](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L89) |
| tma_load_g | LOAD | m_g | TMAQKLayout | [L92](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L92) |
| tma_load_dt_bias | LOAD | m_dt_bias (H,D) fp32 | TMAGTotalSmemLayout | [L94-L96](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L94-L96) |
| tma_store_ws_kd/qd/kr | STORE | m_ws_kd/qd/kr | TMAVOLayout（swizzle） | [L98-L100](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L98-L100) |
| tma_store_ws_gt | STORE | m_ws_gt | TMAGTotalSmemLayout | [L101](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L101) |
| tma_store_ws_inv / _mqk | STORE | m_ws_inv / m_ws_mqk | TMALMLayout | [L102-L103](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L102-L103) |
| tma_load_v | LOAD | m_v | TMAVOLayout | [L106](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L106) |
| tma_load_beta2 | LOAD | m_beta（同型第二份实例） | TMABetaSmemLayout | [L107](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L107) |
| tma_load_ws_kd/qd/kr/gt/inv/mqk | LOAD | m_ws_* | 与 K1 的 STORE 一一对应 | [L109-L114](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L109-L114) |
| tma_store_out | STORE | m_out | TMAVOLayout | [L116](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L116) |

state 描述符的条件构造：[csrc/smxx/fwd_launch.cu:L118-L144](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L118-L144)

```cpp
auto make_state_tma = [&]() {
    if constexpr (StateFP32) {
        // fp32 状态：TMAFP32StateSmemLayout（K_SW32 atom，见 fwd_kernel2.cuh L60-L69）
        ...
        return cute::make_tuple(tma_load, tma_store);
    } else {
        auto state_ptr_load = HasStateIn  ? static_cast<BF16 const*>(initial_state_ptr)
                                : reinterpret_cast<BF16 const*>(out_ptr);  // dummy, never used
        auto state_ptr_store = HasStateOut ? static_cast<BF16*>(final_state_ptr)
                                : reinterpret_cast<BF16*>(out_ptr);        // dummy, never used
        ...
    }
};
auto [tma_load_initial_state, tma_store_final_state] = make_state_tma();
```

bf16 分支用 `out_ptr` 做哑指针（源码注释 `dummy, never used`）：kernel 的模板参数列表**无条件**接收这两个描述符对象，因此即便 `HasStateIn=false` 也必须给出可构造的对象；使用处被 `if constexpr (HasStateIn ...)` 守卫（如 [csrc/smxx/fwd_kernel2.cuh:L241-L243](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L241-L243)）。一个值得注意的不对称：fp32 分支没有做哑指针替换——当「只有 final_state 且为 fp32」时 `initial_state_ptr` 是 nullptr（[csrc/flash_kda.cpp:L141-L142](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L141-L142)），描述符会以 null 基地址编码；测试矩阵覆盖了全部状态组合（[tests/test_fwd_full.py:L48-L56](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test_fwd_full.py#L48-L56) 的 4 种 IO × 2 种 dtype），说明这在目标环境可行，但两种分支为何区别处理、null 基地址跨驱动版本是否有保证——源码未注释（待确认）。

**K1 启动块**（[csrc/smxx/fwd_launch.cu:L146-L181](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L146-L181)）：

```cpp
constexpr int kK1Threads = 256;
int smem_size_k1 = sizeof(SharedStorageK1T);
auto kernel1 = _flash_kda_fwd_prepare< ...11 个描述符类型..., CHUNK, D, kK1Threads, IsVarlen>;
cudaFuncSetAttribute(kernel1, cudaFuncAttributeMaxDynamicSharedMemorySize, smem_size_k1);
if constexpr (IsVarlen) {
    _flash_kda_build_tile_prefix<<<1, 32, 0, stream>>>(cu_seqlens_ptr, N, CHUNK, ws_tile_prefix);
}
dim3 grid_k1(total_tiles, H);
kernel1<<<grid_k1, block_k1, smem_size_k1, stream>>>(...);
```

- grid.x 是**启动口径**的 total_tiles。varlen 下它是上界，多出来的 CTA 在 kernel 内 early-return（[csrc/smxx/fwd_kernel1.cuh:L198-L199](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L198-L199)）——用一点启动开销换 host 端无需把设备上的 cu_seqlens 读回来算精确 tile 数。
- `blockIdx.x → global_tile_idx`、`blockIdx.y → head_idx`（[csrc/smxx/fwd_kernel1.cuh:L169-L170](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L169-L170)）。
- tile_prefix 前置 kernel 只有 1 个 block、32 线程、单线程工作（[csrc/smxx/fwd_kernel1.cuh:L89-L104](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L89-L104)），与 K1 同 stream，顺序执行有保证。
- 11 个描述符以 `CUTE_GRID_CONSTANT`（kernel 参数空间）传给 kernel（[csrc/smxx/fwd_kernel1.cuh:L120-L131](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L120-L131)），`__launch_bounds__(NumThreads, 8)` 是 occupancy 提示（u2-l8 详述）。

**K2 启动块**（[csrc/smxx/fwd_launch.cu:L183-L216](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L183-L216)）：

```cpp
constexpr int kK2Threads = 32 * 2 + 128;   // 4 个 MMA warp + 1 LOAD warp + 1 STORE warp = 192
dim3 grid_k2(N, H);
kernel2<<<grid_k2, block_k2, smem_size_k2, stream>>>(...);
```

- `blockIdx.x → seq_idx`、`blockIdx.y → head_idx`（[csrc/smxx/fwd_kernel2.cuh:L216-L217](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L216-L217)）。grid.x 是 **N**（序列条数）而非 tile 数——这是两级流水线并行结构的分水岭。
- `kInputStages=3 / kOutputStages=2`（[csrc/smxx/fwd_launch.cu:L29-L30](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L29-L30)）是 K2 流水线深度，作为模板参数传入（u3-l2 展开）。
- smem 规模对比（按布局推导的估算值，精确值以 `--ptxas-options=-v` 输出为准，setup.py 已默认开启该选项 [setup.py:L79](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/setup.py#L79)）：

| | K1 | K2 |
| --- | --- | --- |
| grid | (total_tiles, H) | (N, H) |
| block | 256 线程 | 192 线程（4 MMA + 1 LOAD + 1 STORE） |
| 并行轴 | tile × head 全并行 | (序列, head) 并行，序列内 tile 串行 |
| 动态 smem | ≈21 KB（union 复用后） | ≈98 KB（state 32K + union max(62K, 64K)） |
| 超 48 KB 需 opt-in | 否（仍统一设置） | **是** |

K2 的 `cudaFuncSetAttribute`（[csrc/smxx/fwd_launch.cu:L201](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L201)）是**必须的**；K1 的（[L162](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L162)）未超 48 KB，一并设置保持代码一致。

**`BLOCK_LEVEL_K1/K2` 宏**：默认值为 1（[csrc/smxx/utils.cuh:L30-L36](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L30-L36)），两个启动块分别被 `#if BLOCK_LEVEL_K1 >= 0`（[L147](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L147)）与 `#if BLOCK_LEVEL_K2 >= 0`（[L184](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L184)）包住——设为 -1 即在编译期整体裁掉该 kernel 的启动，是结构消融实验的开关（setup.py 没有提供注入 `-D` 的环境变量，需要手工在 `extra_compile_args['nvcc']` 里追加，见 [setup.py:L70-L83](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/setup.py#L70-L83)；完整消融实践见 u3-l12）。

最后，文件末尾的显式实例化表（[csrc/smxx/fwd_launch.cu:L219-L238](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L219-L238)）用宏展开出 7 种状态组合 × varlen/非 varlen 共 14 份 `launch_fwd` 实例——u2-l3 讲过的分发链在这里闭环。

#### 4.3.4 代码实践

**目标**：建立「描述符 ↔ kernel ↔ 方向」的清单直觉，并用剖析工具实测 grid。

1. **源码阅读型**：对照上面的描述符表，数一数两个 kernel 各收到多少个 TMA 描述符。K1 签名（[csrc/smxx/fwd_kernel1.cuh:L120-L131](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L120-L131)）应为 5 LOAD + 6 STORE = 11 个；K2 签名（[csrc/smxx/fwd_kernel2.cuh:L133-L144](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L133-L144)）应为 8 LOAD + 2 state + 1 STORE(out) = 11 个。逐个标注方向与 gmem 来源。
2. **实测型（需 GPU，待本地验证）**：跑一次任意调用（如 u1-l5 的 mini_run.py），用 nsys 查看 kernel 名与 grid：

   ```bash
   nsys profile -o /tmp/kda --stats=true python mini_run.py
   # 在 cuda_gpu_trace / cuda_gpu_kern_sum 报告中找
   #   _flash_kda_fwd_prepare    grid (total_tiles, H, 1)
   #   _flash_kda_fwd_recurrence grid (N, H, 1)
   ```

   **观察现象**：对 batched B=1、T=4096、H=32 的输入，prepare 的 grid.x 应为 256（精确值），recurrence 的 grid.x 应为 1；换成 seq_lens=[7,33] 的 varlen 输入，prepare 的 grid.x 变为上界 \(\lceil 40/16\rceil+2=5\)（实际 tile 数 1+3=4，多出的 1 个块 early-return），recurrence 的 grid.x 为 2。**预期结果**：grid 尺寸与本讲公式一致；若不一致，优先检查你传的是分配口径还是启动口径。

#### 4.3.5 小练习与答案

1. **练习**：为什么 v 的 smem 布局是 swizzle 的 `TMAVOLayout`，而 q/k/g 用自然的 `TMAQKLayout`？
   **答案**：消费方式不同。K2 里 v 作为 MMA 的操作数，LDSM 从 smem 读取时要求 GMMA/LDSM 友好的 swizzle 排布（u2-l4 的 K_INTER atom）；K1 里 q/k/g 由线程按 (t, d) 逐元素做 decay/cumsum，自然行主序的寻址最简单直接。TMA 描述符的 smem 布局就是为「消费者怎么读」定制的。
2. **练习**：varlen 模式下 K1 的 grid.x 为什么宁可偏大也不算精确？
   **答案**：cu_seqlens 在设备内存里。host 要算精确 tile 数就得做一次 D2H 同步拷贝，破坏流水线并引入延迟；而多启动的块只浪费极小的启动开销、进 kernel 即返回（[csrc/smxx/fwd_kernel1.cuh:L198-L199](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L198-L199)）。workspace 也用同一个上界公式分配，两边自洽。
3. **练习**：删掉 K2 的 `cudaFuncSetAttribute` 会发生什么？
   **答案**：K2 动态 smem 约 98 KB，超过默认 48 KB 上限，kernel 启动会直接失败（典型报错是 `invalid argument` 类的 cudaError；具体错误码待本地验证）。K1 约 21 KB 不受影响——这解释了为什么两处都写但只有 K2 是「必须」。

## 5. 综合实践：workspace_layout.py 对拍

**任务**：写 `workspace_layout.py`，对 \((T_{total}=8200, H=96)\) 的 batched（N=1）与 varlen（N=8）两种模式，复现 `get_workspace_size` 公式、打印七段（六数组 + tile_prefix）的起始偏移与总字节数，并与 C++ 函数返回值对拍。`get_workspace_size` 是纯 host 运算，对拍不需要 GPU，但需要已编译安装的包。

```python
# workspace_layout.py（示例代码）
CHUNK, D = 16, 128

# 每段字节，顺序与 csrc/smxx/fwd_launch.cu 的切分一致（utils.cuh WorkspaceSizes）
SEGMENTS = [
    ("k_decayed ", CHUNK * D * 2,     "bf16"),
    ("q_decayed ", CHUNK * D * 2,     "bf16"),
    ("k_restored", CHUNK * D * 2,     "bf16"),
    ("g_total   ", D * 4,             "fp32"),
    ("INV       ", CHUNK * CHUNK * 2, "bf16"),
    ("Mqk       ", CHUNK * CHUNK * 2, "bf16"),
]
PER_TILE = sum(s for _, s, _ in SEGMENTS)
assert PER_TILE == 13824

def alloc(T_total, H, N):                      # 复现 get_workspace_size（分配口径）
    tiles = (T_total + CHUNK - 1) // CHUNK + N
    prefix = ((N + 1) * 4 + 127) // 128 * 128
    return tiles, H * tiles * PER_TILE + prefix, prefix

def launch_tiles(T_total, N, batched):         # 复现 fwd 内的启动口径
    if batched:
        return N * ((T_total // N + CHUNK - 1) // CHUNK)   # 此处 B=1, T_seq=T_total
    return (T_total + CHUNK - 1) // CHUNK + N

def report(T_total, H, N, batched):
    tiles_a, total, prefix = alloc(T_total, H, N)
    tiles_l = launch_tiles(T_total, N, batched)
    n_ht = H * tiles_l
    tag = "batched" if batched else "varlen "
    print(f"== {tag} T_total={T_total} H={H} N={N} | "
          f"分配 tiles={tiles_a} 启动 tiles={tiles_l} n_ht={n_ht}")
    off = 0
    for name, seg, dt in SEGMENTS:
        size = n_ht * seg
        assert off % 128 == 0
        print(f"   {name}[{dt}] offset={off:>12,} size={size:>12,} end={off+size:>12,}")
        off += size
    print(f"   tile_prefix     offset={off:>12,} size={prefix:>12,}")
    used = off + prefix
    print(f"   启动占用 {used:,} B ({used/2**20:.2f} MiB)  "
          f"分配 {total:,} B ({total/2**20:.2f} MiB)")
    assert used <= total
    return total

if __name__ == "__main__":
    want = {}
    want[("batched", 1)] = report(8200, 96, 1, batched=True)
    want[("varlen",  8)] = report(8200, 96, 8, batched=False)
    try:
        from flash_kda_C import get_workspace_size
        for (mode, N), py in want.items():
            cpp = get_workspace_size(8200, 96, N)
            print(f"[对拍] {mode}: C++={cpp:,} python={py:,} "
                  f"{'一致' if cpp == py else '不一致!!'}")
    except ImportError:
        print("[对拍] 未安装 flash_kda_C，跳过对拍 —— 待本地验证")
```

**操作步骤**：

1. 在仓库根目录运行 `python workspace_layout.py`。
2. 核对输出与下面的预期值。
3. 若已 `pip install -e .`，确认两行 `[对拍] ... 一致`。

**预期结果**（关键数值，已于本讲手工核算）：

- batched（N=1）：分配 tiles=514，启动 tiles=513，n_ht=49,248。
  k_decayed offset=0 size=201,719,808；q_decayed offset=201,719,808；k_restored offset=403,439,616；g_total offset=605,159,424 size=25,214,976；INV offset=630,374,400；Mqk offset=655,589,376（结束于 680,804,352）；tile_prefix offset=680,804,352 size=128。
  启动占用 680,804,480 B（649.27 MiB）≤ 分配 682,131,584 B（650.53 MiB），松弛恰为 \(H\times13824 = 1{,}327{,}104\) 字节——两种口径差异的实证。
- varlen（N=8）：分配 tiles=启动 tiles=521，n_ht=50,016。
  k_decayed 0 → 204,865,536；q_decayed → 409,731,072；k_restored → 614,596,608；g_total offset=614,596,608 size=25,608,192；INV → 640,204,800；Mqk → 665,812,992（结束于 691,421,184）；tile_prefix size=128。
  启动占用 = 分配 = 691,421,312 B（659.39 MiB）。

**需要观察的现象**：① 每段 offset 都是 128 的倍数（`static_assert` 的运行时投影）；② batched 的启动占用严格小于分配、varlen 的两者相等；③ varlen 比 batched 多 7 条独立序列（N 从 1 到 8），总分配只多了约 8.9 MiB——恰为 \(H\times7\times13824 = 9{,}289{,}728\) 字节，即每条新增序列在每 head 下多备一个尾 tile 槽。

## 6. 本讲小结

- 五个输入/输出张量共用 `(H,T,D):(D, D*H, 1)` 的 gmem 布局——形状步长相同、仅基地址不同；beta 转置后拉平成 1D、state 合并为 `(N*H, D, D)`，都是为 TMA 寻址与对齐服务。
- workspace 是按 `n_ht = H × total_tiles(启动口径)` 的前缀和切成的六段数组（3×4096 + 3×512 = 13824 B/tile）+ 128 字节对齐的 tile_prefix 尾部；`static_assert` 保证每段起始 128 字节对齐。分配口径用上界 \(\lceil T_{total}/16\rceil+N\)，batched 启动口径是精确值，两者之差是每序列一个 tile 槽的松弛。
- 22 个 TMA 描述符各绑定「基地址 + gmem 盒子 + smem 布局」；K1 的 6 个 STORE 与 K2 的 6 个 LOAD 同型同源，构成位一致契约；state 描述符按 `StateFP32` 编译期二选一，无状态时用 `out_ptr` 哑指针。
- K1 grid=(total_tiles, H)、256 线程，tile×head 全并行，varlen 多余块 early-return；K2 grid=(N, H)、192 线程（4 MMA + 1 LOAD + 1 STORE warp），序列内 tile 串行递推——grid 差异是双 kernel 并行结构的直接体现。
- `cudaFuncSetAttribute` 对 K2（≈98 KB smem）是超过 48 KB 上限的必要 opt-in；`BLOCK_LEVEL_K1/K2` 宏是编译期裁掉某个 kernel 的消融开关。

## 7. 下一步学习建议

本讲止步于「kernel 被正确启动」；接下来沿两条线深入：

- **u2-l6（Kernel 1 骨架）**：进入 `_flash_kda_fwd_prepare` 内部——本讲的 tile_prefix 数组如何被 O(log N) 二分查找消费、单线程单发的 TMA 加载如何与事务 barrier 配合。
- **u2-l8（workspace 契约）**：从 K1 末尾的 6 次 TMA store 与 K2 load warp 的对称读取，把本讲的「位一致契约」落成逐比特的读写地址公式（`ws_idx = head × total_tiles + tile`）。
- 想先看剖析工具实操的读者可跳读 u3-l10（bench 与 ncu），再回来做本讲 4.3.4 的 grid 实测。
