# u2-l8 workspace 契约：K1 如何写、K2 如何读

## 1. 本讲目标

学完本讲，你应该能够：

1. 写出 workspace 六个中间分量（k_decayed / q_decayed / k_restored / g_total / INV / Mqk）各自的形状、dtype、字节数，以及 K1 写入与 K2 读取所共用的地址公式 \( \text{ws\_idx} = \text{head} \times \text{total\_tiles} + \text{tile} \)。
2. 解释 K1 末尾六次 TMA store 的执行细节：为什么全部由线程 0 发起、`tma_store_arrive` / `tma_store_wait<0>` 如何收尾、store 之前为什么要加代理围栏。
3. 解释 K2 的 LOAD warp 如何以完全对称的方式把同一份比特读回 smem，包括 varlen 模式下 `tile_base` 与 K1 侧 `tile_prefix` 二分查找的对应关系。
4. 解释 `SharedStorageK1` 中三个 union 的生命周期复用策略，算出「Phase A / Phase B」union 省下的约 14 KB smem。
5. 理解 K1 用 `__launch_bounds__(256, 8)` 把寄存器压到每线程 32 个、换满线程占用的取舍。

## 2. 前置知识

- **workspace 是什么**：FlashKDA 把前向拆成两个 kernel（见 u1-l4 / u2-l5）。Kernel 1（prepare）按 (tile, head) 全并行计算每个 16-token 块的中间量；Kernel 2（recurrence）按 (序列, head) 并行、序列内沿 tile 串行递推。两个 kernel 之间没有直接通信，全靠一块全局内存缓冲区——workspace——传递每个 tile 的 6 类中间张量。
- **TMA 复习**：SM90 的 TMA（Tensor Memory Accelerator）由「TensorMap 描述符 + `cute::copy`」驱动，一次拷贝可以在 smem 的 swizzle 布局与 gmem 的规范布局之间搬运一整块 tile。`SM90_TMA_STORE` 用于 smem→gmem，`SM90_TMA_LOAD` 用于 gmem→smem（u2-l4 / u2-l5 已详述描述符的构造）。
- **异步代理与围栏**：TMA 读写 smem 走 async proxy，而 kernel 里普通 `st.shared` 走 generic proxy。两个代理的写对彼此不可见，必须用 `cutlass::arch::fence_view_async_shared()` 打通（u2-l6 讲过加载侧；本讲讲 store 侧）。
- **`tma_store_arrive` / `tma_store_wait`**：TMA store 是异步的。`tma_store_arrive()` 把本线程此前发起的 TMA store 提交（commit）到一个完成组；`tma_store_wait<0>()` 阻塞直到所有已提交组真正完成——即数据已经离开 smem、落到 gmem。
- **`__launch_bounds__(maxThreads, minBlocksPerSM)`**：给编译器的提示，声明「每个 CTA 最多这么多线程、且一个 SM 至少要能同时放下这么多 CTA」，编译器据此限制寄存器分配。

## 3. 本讲源码地图

| 文件 | 本讲关注点 |
| --- | --- |
| [csrc/smxx/fwd_kernel1.cuh](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh) | `SharedStorageK1` 的 union 布局（L44-84）；`__launch_bounds__`（L120）；kernel 末尾的六次 TMA store（L515-585） |
| [csrc/smxx/fwd_kernel2.cuh](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh) | LOAD warp 对 workspace 的六次对称读取（L324-422）；`tile_base` 的求法（L221-234）；每 stage 事务字节预算（L172-181） |
| [csrc/smxx/utils.cuh](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh) | `WorkspaceSizes<CHUNK, D>`：六段的每 tile 字节数与 128 字节对齐断言（L64-77） |
| [csrc/smxx/fwd_launch.cu](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu) | host 侧把 workspace 切成六段数组 + tile_prefix 尾部（L62-71）；K1 store 描述符（L98-103）与 K2 load 描述符（L109-114）同源同型 |
| [csrc/flash_kda.cpp](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp) | `get_workspace_size` 的总字节数公式（L5-26）；`total_tiles` 的「分配口径 vs 启动口径」（L176-181） |

## 4. 核心概念与源码讲解

### 4.1 六次 TMA store 与 ws_idx 寻址

#### 4.1.1 概念说明

Kernel 1 的每个 CTA 负责一个 (tile, head)，算完之后要把 6 类中间张量写进 workspace，供 Kernel 2 消费。这 6 类中间量的规格如下（CHUNK=16，D=128）：

| 分量 | 形状（每 tile） | dtype | 字节 | 数学含义（承接 u2-l1 / u2-l7） |
| --- | --- | --- | --- | --- |
| k_decayed | [16, 128] | bf16 | 4096 | \( k \cdot e^{\mathrm{cumsum}(g)} \)，用于擦除项 \( L \) 与 \( u = v - s k^\top \) |
| q_decayed | [16, 128] | bf16 | 4096 | \( q \cdot e^{\mathrm{cumsum}(g)} \cdot \text{scale} \)，用于读出 |
| k_restored | [16, 128] | bf16 | 4096 | \( k \cdot e^{-\mathrm{cumsum}(g)} \cdot e^{g_\text{total}} \)，用于状态写入 |
| g_total | [128] | fp32 | 512 | \( e^{\sum_t g} \)，整块乘性衰减因子（K1 内已做过 ex2 变换） |
| INV | [16, 16] | bf16 | 512 | \( (I+L)^{-1} \)，前代换求逆结果 |
| Mqk | [16, 16] | bf16 | 512 | \( \mathrm{tril}(q_\text{decayed} k_\text{inv}^\top) \)（含对角） |
| **合计** | | | **13824** | `WorkspaceSizes::kPerTile` |

注意 `k_inv`（\( k \cdot e^{-\mathrm{cumsum}(g)} \)）**不在**这份清单里——它只在 K1 片内被用来构造 L 和 Mqk，之后就没有任何消费者，因此不落盘。

workspace 在 gmem 里被组织成**六个分离的三维数组**，每个数组的逻辑形状是 `[H × total_tiles, 16, 128]`（或 `[H × total_tiles, 16, 16]`、`[H × total_tiles, 128]`）。第一个下标就是 `ws_idx`：

\[ \text{ws\_idx} = \text{head\_idx} \times \text{total\_tiles} + \text{global\_tile\_idx} \]

即「同一个 head 的所有 tile 在每段数组里连续排布，head 之间按 `total_tiles` 跳跃」。K1 写它、K2 读它，用的是同一个公式，这是整个契约的核心。

还有一个容易混淆的细节：**`total_tiles` 有两个口径**（见 [csrc/flash_kda.cpp:176-181](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L176-L181)）。

- **分配口径**（Python 层调 `get_workspace_size` 用）：\( \lceil T_\text{total}/16 \rceil + N \)，是上界——varlen 下每条序列至多多算一个尾 tile；
- **启动口径**（launch 层传给两个 kernel 的标量）：varlen 下仍是上界，batched 下是精确值 \( N \times \lceil T_\text{seq}/16 \rceil \)。

关键在于：**K1 和 K2 拿到的是同一个标量**，所以 `ws_idx` 的步长（head 之间的跳跃）两边完全一致；分配口径只要 ≥ 启动口径即可，多出来的部分只是闲置。

#### 4.1.2 核心流程

K1 计算主体（L2 归一化 → 门控 cumsum → decay_apply → L/Mqk → 求逆）全部完成后，收尾流程是：

```text
inv_fwd_subst_fused_1warp(...)          # INV 写入 smem（STSM，generic proxy 写）
fence_view_async_shared()               # ① generic 写 → 对 async proxy（TMA）可见
__syncthreads()                         # ② 全块到齐，INV/L/Mqk 数据就绪
if (threadIdx.x == 0):
    ws_idx = head_idx * total_tiles + global_tile_idx
    for 每个分量 in [k_decayed, q_decayed, k_restored, g_total, INV, Mqk]:
        构造 gmem tile 张量（基地址 = 段基址 + ws_idx 对应偏移）
        构造 smem 源张量（带 TMA swizzle 布局）
        cute::copy(tma_store_xxx, smem, gmem)   # 异步发起
        tma_store_arrive()                      # 提交完成组
tma_store_wait<0>()                     # 等六次 store 全部落到 gmem
__syncthreads()
```

要点：

1. **只有线程 0 发起 store**——与 u2-l6 讲过的「单线程发起全部 TMA load」完全对称。TMA 拷贝由描述符描述，不需要 256 个线程一起搬。
2. **六次而非一次**：六个 smem 缓冲在 smem 中互不连续、在 gmem 中也是六个独立数组，所以必须六个描述符、六次拷贝。
3. **围栏在 store 之前**：计算阶段的所有 smem 写（包括求逆的 STSM 写 INV）都是 generic proxy 写，TMA 读 smem 走 async proxy，必须先 `fence_view_async_shared()` 打通可见性。（提交 5fdc7b2「fix missing proxy fences around TMA accesses」就是补齐了同类遗漏的围栏。）

#### 4.1.3 源码精读

先看围栏与同步的衔接，[csrc/smxx/fwd_kernel1.cuh:511-514](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L511-L514)：求逆结果 INV 刚由 STSM 写进 smem，紧接一道代理围栏加一次块同步，才允许线程 0 开始搬。

```cpp
inv_fwd_subst_fused_1warp(L_fp32, M_bf16, INV, compute_tid);
// Fence + sync combined: completion + TMA visibility
cutlass::arch::fence_view_async_shared();
__syncthreads();
```

然后是六次 store 的第一段（其余五段结构完全相同），[csrc/smxx/fwd_kernel1.cuh:515-527](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L515-L527)。先算 `ws_idx`，再把 gmem 张量的基地址平移到 `(ws_idx, 0, 0)` 对应的 tile，smem 侧套上 `TMAVOLayout`（swizzle 布局的 TMA 视图，u2-l4），一次 `cute::copy` 搬完 [16,128] 整块，最后 `tma_store_arrive()` 提交：

```cpp
if (threadIdx.x == 0) {
    int ws_idx = head_idx * total_tiles + global_tile_idx;
    // Store k_decayed [CHUNK, D] bf16
    {
        auto g_ws = tma_store_ws_kd.get_tma_tensor(make_shape(H * total_tiles, CHUNK, D));
        auto ws_off = g_ws.layout()(ws_idx, 0, 0);
        Tensor g_ws_tile = make_tensor(g_ws.data() + ws_off, ...);
        Tensor s_kd = make_tensor(make_smem_ptr(shared_storage.k_decayed.begin()), TMAVOLayout{});
        cute::copy(tma_store_ws_kd, cta_tma.partition_S(s_kd), cta_tma.partition_D(g_ws_tile));
        tma_store_arrive();
    }
```

后续五段依次是 q_decayed（[L528-538](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L528-L538)）、k_restored（[L539-549](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L539-L549)）、g_total（[L550-560](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L550-L560)，gmem 形状是 `[H*total_tiles, D]`）、INV（[L561-571](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L561-L571)，布局换成 `TMALMLayout`）、Mqk（[L572-582](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L572-L582)）。最后 [csrc/smxx/fwd_kernel1.cuh:584-585](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L584-L585) 等待全部完成组落地再放行 CTA 退出：

```cpp
tma_store_wait<0>();
__syncthreads();
```

这些 store 的 gmem 目标从哪来？host 侧 [csrc/smxx/fwd_launch.cu:62-71](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L62-L71) 把一整块 workspace 按 `n_ht = H × total_tiles` 份每段大小做前缀和切分：

```cpp
int64_t n_ht = int64_t(H) * total_tiles;
char* ws = reinterpret_cast<char*>(workspace_ptr);
BF16*  ws_kd = reinterpret_cast<BF16*>(ws);
BF16*  ws_qd = reinterpret_cast<BF16*>(ws + n_ht * WS::kKDecayed);
BF16*  ws_kr = reinterpret_cast<BF16*>(ws + n_ht * (WS::kKDecayed + WS::kQDecayed));
...
int* ws_tile_prefix = reinterpret_cast<int*>(ws + n_ht * WS::kPerTile);
```

每段大小来自 [csrc/smxx/utils.cuh:64-77](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L64-L77) 的 `WorkspaceSizes`，三个 `static_assert` 在编译期固化「每段字节数都是 128 的倍数」——这样无论 `n_ht` 是多少，每段基址都继承 workspace 基指针的对齐，满足 TMA 的全局地址对齐要求，段内 tile 定位也不需要任何运行时取整。

总大小公式在 [csrc/flash_kda.cpp:5-26](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L5-L26)：

\[ \text{workspace} = H \times \left(\lceil T_\text{total}/16 \rceil + N\right) \times 13824 + \mathrm{align}_{128}\big((N{+}1)\times 4\big) \]

第二项是 varlen 模式下 tile 前缀和数组 `tile_prefix`（N+1 个 int32，向上对齐到 128 字节）；batched 模式下它仍被分配、但不会被使用（前缀和 kernel 只在 varlen 分支启动，见 [csrc/smxx/fwd_launch.cu:164-167](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L164-L167)）。

#### 4.1.4 代码实践

**实践目标**：用 Python 复现 workspace 的字节账本，并与 C++ 的 `get_workspace_size` 对拍。

**操作步骤**（示例代码）：

```python
# workspace_bytes.py —— 复现 WorkspaceSizes / get_workspace_size 公式
def get_ws_size_py(T_total, H, N, CHUNK=16, D=128):
    per_tile = 3 * (CHUNK * D * 2) + D * 4 + 2 * (CHUNK * CHUNK * 2)  # 13824
    total_tiles = (T_total + CHUNK - 1) // CHUNK + N                  # 分配口径（上界）
    tile_prefix = ((N + 1) * 4 + 127) // 128 * 128
    return H * total_tiles * per_tile + tile_prefix

cases = [(4096, 32, 4), (8200, 96, 8), (1024, 32, 1)]
for T, H, N in cases:
    print(T, H, N, get_ws_size_py(T, H, N))

# 对拍（需要已安装的 SM90 构建环境）：
# import flash_kda_C  # 由 flash_kda 包加载
# assert flash_kda_C.get_workspace_size(T, H, N) == get_ws_size_py(T, H, N)
```

**需要观察的现象**：三组参数下公式输出的字节数；三段 16×128 bf16（各 4096 B）+ 一段 fp32 g_total（512 B）+ 两段 16×16 bf16（各 512 B）合成 13824 B/tile。

**预期结果**：`(4096, 32, 4)` 应得到 \( 32 \times 260 \times 13824 + 128 = 115{,}015{,}808 \) 字节（约 109.7 MiB）。与 C++ 对拍部分**待本地验证**（需要 SM90 机器上 `pip install --no-build-isolation .` 成功）。

#### 4.1.5 小练习与答案

**练习 1**：H=32、启动口径 total_tiles=256，K1 的 CTA `blockIdx=(100, 7)`（x 是 global_tile_idx，y 是 head_idx）。它写入的六个分量在各段数组内的字节偏移各是多少？

**答案**：`ws_idx = 7×256 + 100 = 1892`。k_decayed 段内偏移 \( 1892 \times 4096 = 7{,}749{,}632 \)；q_decayed 段内同偏移（段基址不同）；k_restored 同理；g_total 段内 \( 1892 \times 512 = 968{,}704 \)；INV、Mqk 段内也是 968,704。三段 [16,128] 数组每 tile 跨 4096 B，三个小数组每 tile 跨 512 B。

**练习 2**：为什么 `WorkspaceSizes` 要用 `static_assert` 强制每段字节数是 128 的倍数？

**答案**：段基址 = workspace 基指针 + `n_ht × 前面各段字节数之和`，而 `n_ht` 随 (H, total_tiles) 任意变化。只有每段大小本身是 128 的倍数，所有段基址才能始终落在 128 字节边界上，满足 TMA 对全局地址的对齐要求，同时让「tile → 字节偏移」只是乘法、无需运行时对齐修正。

**练习 3**：g_total 为什么独享 fp32，而其余五个分量都是 bf16？

**答案**：g_total 是乘性整块衰减因子 \( e^{\sum g} \)，K2 的状态更新（Phase 6）里它与 bf16 状态做 fp32 FMA：\( s \leftarrow \mathrm{bf16}(s \cdot g_\text{total} + \delta s) \)。把它保持 fp32 可以避免在「每个 tile 都要乘一次」的衰减路径上引入额外量化点；512 B 又恰好满足 128 对齐。其余分量直接作为 HMMA 的 bf16 操作数，存 bf16 即是最终精度。

### 4.2 K2 侧的对称读取

#### 4.2.1 概念说明

Kernel 2 的一个 CTA 负责一个 (seq_idx, head_idx)，沿本序列的 tile 串行递推。每个 tile 迭代需要 8 份数据：v、beta（原始输入）+ 六个 workspace 分量（K1 产物）。这件事由专职的 **LOAD warp**（192 线程中的第 5 个 warp；角色划分见 [csrc/smxx/fwd_kernel2.cuh:188-197](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L188-L197)，详见 u3-l2）完成。

K2 侧的地址公式与 K1 只差一件事：K2 不知道「全局 tile 编号」，它知道 (seq_idx, 局部 t)，所以要先把前序序列的 tile 数累加出来：

\[ \text{tile\_base} = \sum_{i < \text{seq\_idx}} \lceil \mathrm{len}_i / 16 \rceil, \qquad \text{ws\_idx} = \text{head\_idx} \times \text{total\_tiles} + \text{tile\_base} + t \]

「位一致契约」成立的原因很朴素：**两边引用的是同一批 gmem 张量、同一套布局类型**。host 侧用同一组 `m_ws_*` 张量（[csrc/smxx/fwd_launch.cu:79-84](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L79-L84)）分别构造 K1 的六个 `SM90_TMA_STORE` 描述符（[L98-103](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L98-L103)）与 K2 的六个 `SM90_TMA_LOAD` 描述符（[L109-114](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L109-L114)），且 TMA smem 布局类型（`TMAVOLayout` / `TMALMLayout` / `TMAGTotalSmemLayout`）在 K1Layouts 与 K2Layouts 中由完全相同的表达式构造，是同一个 C++ 类型。于是：gmem 侧按规范 row-major 落盘，smem 侧用同一 swizzle 解释——**K1 写下的比特就是 K2 读到的比特**，中间不存在任何格式转换。

还有一个正确性细节：varlen 下启动口径 `total_tiles` 是上界，K1 中超过实际 tile 数的 CTA 在任何加载/写入之前就 early return（[csrc/smxx/fwd_kernel1.cuh:199](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L199)），这些 `ws_idx` 槽位是「洞」；而 K2 只迭代 `t < t_tiles`（本序列的真实 tile 数），永远不会踩进洞里。写入者与读者对「实际 tile 集合」的判定完全一致。

#### 4.2.2 核心流程

LOAD warp 的主循环（每 tile 一次迭代）：

```text
for t in [0, t_tiles):
    load_pipeline.producer_acquire(stage)        # 等一个空的输入 stage（3 级缓冲）
    ws_idx = head_idx * total_tiles + tile_base + t
    在同一个事务 barrier 上发起 8 份 TMA 拷贝：
        v       ← 原始 v      （[H,T_total,D] gmem，非 workspace）
        beta    ← 原始 beta   （1D，按 &~7 对齐，见 u2-l6）
        k_decayed / q_decayed / k_restored ← workspace 三段
        g_total ← workspace；INV / Mqk ← workspace
    ++load_write（stage 环形推进）
load_pipeline.producer_tail(...)                 # 收尾
```

8 份拷贝共享**一个**事务 barrier，完成条件是「累计到达的事务字节 = kTmaTransactionBytes」。这个预算是编译期常量（[csrc/smxx/fwd_kernel2.cuh:172-181](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L172-L181)）：

\[ 2048{\times}2 + 32{\times}2 + 2048{\times}2{\times}3 + 128{\times}4 + 256{\times}2{\times}2 = 17{,}984 \ \text{字节/stage} \]

（v 4096 + beta 64 + 三段大矩阵 12288 + g_total 512 + INV/Mqk 1024。）

与 K1 的两点对照值得注意：

1. **发起者**：K1 是 `threadIdx.x == 0`（整块唯一生产者）；K2 的 LOAD warp 用 `lane_predicate = elect_one_sync()`（warp 内选出一个 lane，[csrc/smxx/fwd_kernel2.cuh:237](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L237)）——因为 K2 有多个 warp，加载职责被专门化了。
2. **流水线**：K1 单发（一次 barrier、一锤子买卖）；K2 是 `InputStages=3` 的多级环形流水线（`producer_acquire/consumer_release` 配对），MMA warp 消费第 t 级时 LOAD warp 已经在预取 t+1、t+2 级。

#### 4.2.3 源码精读

先看 `tile_base` 的两种求法，[csrc/smxx/fwd_kernel2.cuh:221-234](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L221-L234)：

```cpp
if constexpr (IsVarlen) {
    bos = cu_seqlens[seq_idx]; eos = cu_seqlens[seq_idx + 1];
    tile_base = 0;
    for (int i = 0; i < seq_idx; i++)          // K2：O(seq_idx) 线性扫描
        tile_base += (int(cu_seqlens[i + 1] - cu_seqlens[i]) + CHUNK - 1) / CHUNK;
} else {
    int T_seq = T_total / N;
    bos = seq_idx * T_seq; eos = bos + T_seq;
    tile_base = seq_idx * ((T_seq + CHUNK - 1) / CHUNK);   // batched：一次乘法
}
```

注意一个有趣的**不对称**：K1 在 varlen 下用辅助 kernel 预计算的 `tile_prefix` 做二分查找（u2-l6），而 K2 却用每 CTA 一次的线性扫描。原因在于规模：K1 的 CTA 数量是 `total_tiles × H`（海量，每个都要求 `global_tile_idx → seq_idx` 的反向映射，必须 O(log N)）；K2 的 CTA 数量只有 `N × H`，而且做的是正向映射（只需累加前序序列），线性扫一遍 cu_seqlens 完全可接受。

然后是读取主循环，[csrc/smxx/fwd_kernel2.cuh:346-351](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L346-L351)：先拿 stage 与事务 barrier，再算 `ws_idx`：

```cpp
for (int t = 0; t < t_tiles; ++t) {
    load_pipeline.producer_acquire(load_write);
    LoadPipelineState::ProducerBarrierType* tma_barrier = load_pipeline.producer_get_barrier(load_write);
    int stage = load_write.index();
    int ws_idx = head_idx * total_tiles + tile_base + t;
```

六次 workspace 读取的第一次（k_decayed，[csrc/smxx/fwd_kernel2.cuh:370-377](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L370-L377)），与 K1 的 store 逐行对偶——gmem 张量形状 `[H*total_tiles, CHUNK, D]`、偏移 `(ws_idx, 0, 0)`、smem 布局 `TMAVOLayout`，只是方向反过来：

```cpp
{
    auto off = g_ws_kd.layout()(ws_idx, 0, 0);
    Tensor g_tile = make_tensor(g_ws_kd.data() + off,
        make_layout(make_shape(Int<1>{}, Int<CHUNK>{}, Int<D>{}), stride(g_ws_kd.layout())));
    Tensor s_tile = make_tensor(make_smem_ptr(shared_storage.input[stage].k_decayed.begin()), TMAVOLayout{});
    cute::copy(tma_load_ws_kd.with(*tma_barrier), cta_ws_kd.partition_S(g_tile), cta_ws_kd.partition_D(s_tile));
}
```

其余五段：q_decayed（[L378-385](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L378-L385)）、k_restored（[L386-393](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L386-L393)）、g_total（[L394-401](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L394-L401)）、INV（[L402-409](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L402-L409)）、Mqk（[L410-417](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L410-L417)）。读取的目标缓冲在 `InputStorage`（[csrc/smxx/fwd_kernel2.cuh:84-93](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L84-L93)）里逐 stage 重复一份，六个成员的布局类型与 K1 的 SharedStorageK1 一一相同。

写成一张对称表（行号均为各自文件内）：

| 分量 | K1 写（fwd_kernel1.cuh） | K2 读（fwd_kernel2.cuh） | TMA smem 布局 | gmem 张量形状 |
| --- | --- | --- | --- | --- |
| k_decayed | L517-527 | L370-377 | TMAVOLayout | [H×total_tiles, 16, 128] |
| q_decayed | L528-538 | L378-385 | TMAVOLayout | [H×total_tiles, 16, 128] |
| k_restored | L539-549 | L386-393 | TMAVOLayout | [H×total_tiles, 16, 128] |
| g_total | L550-560 | L394-401 | TMAGTotalSmemLayout | [H×total_tiles, 128] |
| INV | L561-571 | L402-409 | TMALMLayout | [H×total_tiles, 16, 16] |
| Mqk | L572-582 | L410-417 | TMALMLayout | [H×total_tiles, 16, 16] |
| 寻址 | `head*total_tiles + global_tile_idx` | `head*total_tiles + tile_base + t` | — | 同一批 `m_ws_*` 张量 |

#### 4.2.4 代码实践

**实践目标**：验证「K1 写侧与 K2 读侧的 ws_idx 严格互逆」——即对每个 (seq, t, head)，K2 读取的槽位恰好是 K1 某个 CTA 写过的槽位。

**操作步骤**（示例代码，纯 CPU 即可运行）：

```python
# ws_crosscheck.py —— 模拟 K1 写侧与 K2 读侧的 workspace 寻址
def k1_writer(seq_lens, H, chunk=16):
    """模拟 K1：对每个 (global_tile_idx, head) 给出 (seq, local_t) 与 ws_idx。"""
    exact = [(l + chunk - 1) // chunk for l in seq_lens]
    prefix = [0]
    for e in exact: prefix.append(prefix[-1] + e)
    launch_tiles = prefix[-1]                 # 启动口径（本例直接取精确值）
    out = {}
    for head in range(H):
        for g in range(launch_tiles):
            seq = max(i for i in range(len(seq_lens)) if prefix[i] <= g)  # 二分等价
            local = g - prefix[seq]
            if local < exact[seq]:            # 其余 CTA early-return，不写
                out[(seq, local, head)] = head * launch_tiles + g
    return out, launch_tiles

def k2_reader(seq_lens, H, chunk=16):
    _, launch_tiles = k1_writer(seq_lens, H)  # 复用同一 total_tiles
    out = {}
    for head in range(H):
        for seq, l in enumerate(seq_lens):
            base = sum((x + chunk - 1) // chunk for x in seq_lens[:seq])
            for t in range((l + chunk - 1) // chunk):
                out[(seq, t, head)] = head * launch_tiles + base + t
    return out

w, _ = k1_writer([7, 33, 16, 64], H=2)
r = k2_reader([7, 33, 16, 64], H=2)
print("write slots:", len(w), "read slots:", len(r), "identical:", w == r)
```

**需要观察的现象**：写入槽位集合与读取槽位集合是否逐键相等；varlen 下每条序列长度不是 16 的倍数时映射是否仍然闭合。

**预期结果**：`identical: True`。写成与读侧对 `(seq, t, head) → ws_idx` 完全一致；多余的启动口径槽位（上界多出来的部分）两边都不会触碰。本实践为纯 Python 模拟，可直接运行验证；若要与真实 kernel 行为对拍（如打印某 tile 的 g_total），**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：varlen 输入 `cu_seqlens = [0, 7, 40, 56, 120]`（四条序列，长度 7/33/16/64），启动口径 total_tiles = ⌈120/16⌉ + 4 = 12。K2 处理 (seq_idx=3, head_idx=2, t=2) 时读的 ws_idx 是多少？K1 的哪个 CTA 写了它？

**答案**：各序列 tile 数为 1/3/1/4，`tile_base(3) = 1+3+1 = 5`，`ws_idx = 2×12 + 5 + 2 = 31`。K1 侧：全局 tile 31 - 2×12 = 7，即 `blockIdx=(7, 2)`；二分 `tile_prefix=[0,1,4,5,9]`，7 ∈ [5,9) → seq 3、local_t = 7-5 = 2，与 K2 的读取点一一对应。

**练习 2**：为什么 K2 敢用「线性扫描 cu_seqlens」求 tile_base，而 K1 必须用 tile_prefix + 二分？

**答案**：K1 的映射方向是 `global_tile_idx → seq_idx`（反向），且 CTA 数为 total_tiles×H（可能成千上万），每个 CTA 线性扫一遍是 O(total_tiles×H×N)；二分把它降到 O(log N)。K2 的映射方向是正向（只累加前序序列），CTA 数仅 N×H，每个 CTA 扫 O(seq_idx) 次，总量可忽略。两种方案都遵守同一契约：tile_base（K2）与 tile_prefix（K1）给出相同的全局 tile 编号。

**练习 3**：如果把 K2 侧的 `TMAVOLayout` 换成一个行主、无 swizzle 的普通布局（其余不变），会发生什么？

**答案**：gmem 侧的落盘顺序仍由 K1 的描述符决定（规范 row-major），但 K2 的 LDSM 会按错误的位置解读 smem——swizzle 布局与 `SM75_U32x4_LDSM_N` 的寄存器映射是配套设计的，布局不匹配意味着 MMA 拿到的矩阵元素排列错乱，结果错误。这正是「两边布局必须逐比特一致」的含义：不只是数据一致，连 smem 内的排布方式都要同一类型。

### 4.3 smem union 复用策略

#### 4.3.1 概念说明

K1 的 CTA 需要的 smem 相当多：输入（q、k、g、beta、dt_bias）、decay 家族产物（k_decayed、q_decayed、k_inv、k_restored）、L/Mqk/INV，一共十几块缓冲。但它们**不是同时活跃**的：

- **Phase A（加载与预处理活跃期）**：q、k（L2 归一化后仍原地存 bf16）、g（激活+cumsum 后的 fp32 累计值）。它们在 decay_apply 把数据读进寄存器之后就死了。
- **Phase B（decay 产物活跃期）**：k_decayed、q_decayed、k_inv、L、INV、Mqk。它们在 decay_apply 写回阶段才诞生。

两组缓冲生命周期不相交，就可以放进同一个 `union`——`SharedStorageK1` 用 C++ 匿名 union 让两组共享同一段 smem。算一笔账（cosize × dtype 大小）：

| | 成员 | 字节 |
| --- | --- | --- |
| Phase A | q（2048×bf16） | 4096 |
| | k（2048×bf16） | 4096 |
| | g（2048×fp32） | 8192 |
| | **小计** | **16384** |
| Phase B | k_decayed / q_decayed / k_inv（各 2048×bf16） | 3×4096 = 12288 |
| | L（256×fp32） | 1024 |
| | INV / Mqk（各 256×bf16） | 2×512 = 1024 |
| | **小计** | **14336** |

union 后取 max = 16384 B，省下 14336 B ≈ **14 KB**——与源码注释「union saves ~14KB shared memory」精确吻合。Phase A 之所以更大，是因为 g 的 cumsum 以 fp32 保存（精度要求），这一项（8192 B）撑大了 Phase A。

除主 union 外还有两个小 union 和一处「手动」复用：

1. `g_bf16`（TMA 加载目标，4096 B）↔ `k_restored`（4096 B）：g 的原始值在门控激活消费完后就死了，k_restored 在 decay_apply 写回阶段才需要；
2. `dt_bias`（512 B）↔ `g_total`（512 B）：dt_bias 在激活时用一次，之后 g_total 复用这块缓冲；
3. 求逆时的合并矩阵 `M_bf16` 干脆不进 union，而是**直接别名到已死的 k_inv 缓冲**上（[csrc/smxx/fwd_kernel1.cuh:490-491](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L490-L491)）——k_inv 在构造 L/Mqk 的两次 MMA 之后就没有消费者了，把 M 暂存进去零成本。

#### 4.3.2 核心流程

union 的安全性靠「**先全部读入寄存器 → 同步 → 再写对侧**」这个两段式保证，decay_apply 的结构（[csrc/smxx/fwd_kernel1.cuh:384-473](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L384-L473)）：

```text
阶段①（读）：256 线程把 q/k/g/g_total 的全部所需元素拷进寄存器数组
            reg_g[4][2] / reg_q[4][2] / reg_k[4][2] / reg_gt[4][2]
__syncthreads()      ← 生命周期分界线（L425）
阶段②（写）：同一批线程把计算结果写进 union 对侧的
            k_decayed / q_decayed / k_inv / k_restored 缓冲
```

如果没有 L425 这道同步，快 warp 可能已开始写 k_decayed（与 q 同地址），慢 warp 还没把 q 读进寄存器——数据竞争。有了它，Phase A 的最后一读与 Phase B 的第一写之间隔着全块屏障，union 才是安全的。

资源账本的另一半是 **`__launch_bounds__(NumThreads, 8)`**（[csrc/smxx/fwd_kernel1.cuh:120](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L120)，NumThreads = 256，见 [csrc/smxx/fwd_launch.cu:149](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L149)）。第二个参数 8 表示「每个 SM 至少要能同时驻留 8 个 CTA」：

- 8 CTA × 256 线程 = 2048 线程 = SM90 每 SM 的线程上限 → **满线程占用**；
- 代价是寄存器预算被压到 \( 65536 / 2048 = 32 \) 个/线程，编译器必须更节俭地分配（decay_apply 的寄存器数组恰好规模不大：4 个 tile × 2 元素的四组数组）；
- smem 侧：每 CTA 约 21 KB（16384 + beta 64 + 第二个 union 4096 + 第三个 union 512 + barrier，加上 alignas 填充），8 份约 166–170 KB，仍低于 SM90 的 228 KB 上限——**真正的约束是寄存器，而不是 smem**。

为什么 K1 愿意付这个代价？因为 K1 是「单发」kernel（u2-l6）：一次 TMA 加载 → 计算 → 一次 TMA store，没有 K2 那样的多级流水线来软隐藏延迟，**延迟隐藏完全依赖大量并发 CTA 互相叠加**。8 CTA/SM 意味着某一 CTA 在等 barrier 时，SM 上还有 7 个 CTA 在算。K2 相反，它约 98 KB 的动态 smem 一个 SM 只放得下 1–2 个 CTA，于是改用 warp 专用化 + 流水线（下一单元主题），并不设 `__launch_bounds__` 的 minBlocks 提示。

#### 4.3.3 源码精读

主 union 的定义，[csrc/smxx/fwd_kernel1.cuh:54-71](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L54-L71)。注释直接写明了两个 Phase 的成员清单；每个成员 `alignas(128)` 保证 TMA 访问的 smem 对齐：

```cpp
// Phase A: q, k, g alive
// Phase B: k_decayed, q_decayed, k_inv, L, INV, Mqk alive
// These don't overlap → union saves ~14KB shared memory
union {
    struct {
        alignas(128) cute::ArrayEngine<BF16, cute::cosize_v<QKLayout>> q;
        alignas(128) cute::ArrayEngine<BF16, cute::cosize_v<QKLayout>> k;
        alignas(128) cute::ArrayEngine<float, cute::cosize_v<GLayout>> g;  // 注意 fp32！
    };
    struct {
        alignas(128) cute::ArrayEngine<BF16, cute::cosize_v<MMALayout>> k_decayed;
        alignas(128) cute::ArrayEngine<BF16, cute::cosize_v<MMALayout>> q_decayed;
        alignas(128) cute::ArrayEngine<BF16, cute::cosize_v<MMALayout>> k_inv;
        alignas(128) cute::ArrayEngine<float, cute::cosize_v<LMLayout>> L;
        alignas(128) cute::ArrayEngine<BF16, cute::cosize_v<LMLayout>> INV;
        alignas(128) cute::ArrayEngine<BF16, cute::cosize_v<LMLayout>> Mqk;
    };
};
```

两个小 union，[csrc/smxx/fwd_kernel1.cuh:75-82](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L75-L82)——注释标出哪个是 TMA 加载目标、哪个是复用者：

```cpp
union {
    alignas(128) cute::ArrayEngine<BF16, cute::cosize_v<QKLayout>> g_bf16;      // TMA load target
    alignas(128) cute::ArrayEngine<BF16, cute::cosize_v<MMALayout>> k_restored;
};
union {
    alignas(128) cute::ArrayEngine<float, cute::cosize_v<GTotalLayout>> dt_bias; // TMA load target
    alignas(128) cute::ArrayEngine<float, cute::cosize_v<GTotalLayout>> g_total;
};
```

decay_apply 中生命周期分界线处的注释，[csrc/smxx/fwd_kernel1.cuh:423-425](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L423-L425)：

```cpp
// Sync before writing to union'd smem (q/k/g → k_decayed/q_decayed/k_inv)
// Safe: all 256 threads enter this if block (compute_tid < 256 always true)
__syncthreads();
```

以及 `M_bf16` 别名复用已死的 k_inv 缓冲，[csrc/smxx/fwd_kernel1.cuh:489-491](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L489-L491)：

```cpp
Tensor INV = make_tensor(make_smem_ptr(shared_storage.INV.begin()), LMLayout{});
// Merge matrix M is staged into the (dead) k_inv smem buffer.
Tensor M_bf16 = make_tensor(make_smem_ptr(shared_storage.k_inv.begin()), LMLayout{});
```

`__launch_bounds__` 与启动配置，[csrc/smxx/fwd_kernel1.cuh:120](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L120) 声明 `__launch_bounds__(NumThreads, 8)`；host 侧用 `sizeof(SharedStorageK1T)` 作为动态 smem 大小并 opt-in，[csrc/smxx/fwd_launch.cu:149-162](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L149-L162)。K1 的 smem 约 21 KB，本不需要超过 48 KB 的 opt-in，这里与 K2 共用同一套「设置属性再启动」的模板写法。

#### 4.3.4 代码实践

**实践目标**：把手算的 K1 smem 账本与编译器报告对拍。

**操作步骤**：

1. 手算：按 4.3.1 的表格累加 `SharedStorageK1` 各成员（含三个 union 取 max、beta 64 B、barrier 8 B 与 alignas 填充），得出估总值（约 21 KB 量级）。
2. 重新编译并观察 ptxas 报告：项目 `setup.py` 自带 `--ptxas-options=-v`（u1-l3 讲过），执行 `pip install --no-build-isolation . 2>&1 | tee build.log`。
3. 在 build.log 里找 `_flash_kda_fwd_prepare` 的条目，记录它报告的 smem 字节数与寄存器数。

**需要观察的现象**：ptxas 报告的 used smem 与手算值的差（应为填充与 barrier 的小开销）；寄存器数是否被压在 ≤ 32 附近（`__launch_bounds__(256, 8)` 的预算）。

**预期结果**：smem 报告值略高于 16384 + 64 + 4096 + 512 的裸和（因 128 对齐填充）；寄存器数明显低于未加 launch_bounds 时的自然值。本实践的编译观察部分**待本地验证**（需要 SM90 构建环境）；手算部分现在即可完成。

#### 4.3.5 小练习与答案

**练习 1**：如果去掉主 union（Phase A、Phase B 各自独立分配，其余不变），K1 每 CTA 的 smem 增加多少？若因此每 SM 只能驻留 6 个 CTA，对 K1 意味着什么？

**答案**：增加 Phase B 的小计 14336 B（约 14 KB）；若再算上另两个 union 的 4096 + 512 B，全部展开约多 18.9 KB。smem 变为约 40 KB 量级后，8 CTA × 40 KB ≈ 320 KB 超过 228 KB 上限，SM 驻留 CTA 数下降，K1 这种靠并发隐藏延迟的单发 kernel 吞吐直接受损——这就是 union 的实际收益。

**练习 2**：`__launch_bounds__(256, 8)` 隐含的寄存器上限是多少？为什么 K1 接受这个限制？

**答案**：SM90 每 SM 有 65536 个 32 位寄存器，8 CTA × 256 线程 = 2048 线程，上限 \( 65536/2048 = 32 \) 个/线程。K1 没有多级流水线，延迟隐藏全靠 occupancy；宁可让编译器省着用寄存器（可能少量 spill），也要换满线程占用。K2 的策略正相反（大 smem + 流水线，不设该提示）。

**练习 3**：Phase A 的 g 以 fp32 存储（8192 B），如果为了 smem 改成 bf16 存储，会破坏什么？

**答案**：g 存的是门控激活后的**累计和** cumsum(g)，decay_apply 用它计算 \( e^{\mathrm{cumsum}} \) 与 \( e^{-\mathrm{cumsum}} \)。cumsum 的值域较宽（受 lower_bound ≈ -5 与 CHUNK=16 约束，见 u1-l2 / u3-l8），对其先量化再取幂会把相对误差直接乘进 k_decayed/k_inv 的每个元素，破坏 torch_ref 逐操作复刻的 bit-exact 精度设计。union 已经让 Phase A 的 8192 B 「免费」复用，没有动机冒这个险。

## 5. 综合实践：画一张 workspace 内存图

**任务**：以 H=32、T_total=4096、batched 模式（取 B=4、每条 T_seq=1024，这样 N=4、seq_idx=2 才存在）为例，按比例画出 workspace 的六段数组与 tile_prefix 尾部，并标出 K2 处理 (seq_idx=2, head_idx=5, t=4) 时读取的位置。

**步骤**：

1. **定两个口径**：分配口径 total_tiles = ⌈4096/16⌉ + 4 = 260（Python 层分配用）；启动口径 total_tiles = 4 × ⌈1024/16⌉ = 256（K1/K2 寻址用）。本图按启动口径画「实际使用区」，尾部再画闲置 slack 与 tile_prefix。
2. **算段偏移**：n_ht = 32 × 256 = 8192；三段大数组各 8192 × 4096 = 32 MiB；g_total/INV/Mqk 各 8192 × 512 = 4 MiB。
3. **算标注点**：tiles_per_seq = 64，tile_base(2) = 2 × 64 = 128；ws_idx = 5 × 256 + 128 + 4 = **1412**；在 k_decayed 段内偏移 1412 × 4096 = 5,787,648 B ≈ 5.52 MiB（即 8192 行中的第 1412 行，约在段内 17% 处）。K1 侧对应 CTA：`blockIdx = (global_tile_idx = 2×64+4 = 132, head_idx = 5)`，ws_idx = 5×256 + 132 = 1412，两边闭合。
4. **按比例作图**——用 1 字符 = 1 MiB 的比例逐段画出（▲ 标出 K2 的读取点）：

```text
workspace
┌ k_decayed   [█████▲██████████████████████████]  0    .. 32   MiB   ← ▲ = ws_idx 1412，段内偏移
│                                                        1412×4096 = 5,787,648 B ≈ 5.52 MiB
├ q_decayed   [████████████████████████████████]  32   .. 64   MiB
├ k_restored  [████████████████████████████████]  64   .. 96   MiB
├ g_total     [████]                                96   .. 100  MiB   (fp32)
├ INV         [████]                                100  .. 104  MiB
├ Mqk         [████]                                104  .. 108  MiB
├ slack       [▒]                                   108  .. 109.7 MiB   分配口径 260 − 启动口径 256 tiles
└ tile_prefix [·]                                   109.7 MiB + 128 B  align128(5×4)，batched 下不使用
```

图中读取点的来历：tiles_per_seq = 64，tile_base(2) = 2 × 64 = 128，
ws_idx = head 5 × total_tiles 256 + 128 + t 4 = **1412**；它落在 k_decayed 段第 1412 行（共 8192 行）。
K1 侧的写入者是 `blockIdx = (global_tile_idx = 2×64+4 = 132, head_idx = 5)`，ws_idx = 5×256 + 132 = 1412，两侧闭合。

5. **脚本化验证**（示例代码）：

```python
# workspace_map.py —— 打印上面这张图的精确数值表
def ws_map(T_seq, B, H, CHUNK=16):
    N, T_total = B, B * T_seq
    launch_tiles = N * ((T_seq + CHUNK - 1) // CHUNK)
    n_ht = H * launch_tiles
    alloc_tiles = (T_total + CHUNK - 1) // CHUNK + N
    segs = [("k_decayed", 4096), ("q_decayed", 4096), ("k_restored", 4096),
            ("g_total", 512), ("INV", 512), ("Mqk", 512)]
    off = 0
    for name, per in segs:
        print(f"{name:12s} bytes [{off:>12,} .. {off + n_ht*per:>12,})")
        off += n_ht * per
    prefix_off = H * alloc_tiles * 13824
    print(f"slack ends at {prefix_off:,}; tile_prefix [{prefix_off:,} .. {prefix_off + ((N+1)*4+127)//128*128:,})")

ws_map(T_seq=1024, B=4, H=32)
# 标注点：ws_idx = 5*256 + 2*64 + 4 = 1412 → k_decayed 偏移 1412*4096
```

**预期结果**：表格给出 [0, 33554432) / [33554432, 67108864) / [67108864, 100663296) / [100663296, 104857600) / [104857600, 109051904) / [109051904, 113246208) 六段（字节），slack 结束于 115015680，tile_prefix 占 [115015680, 115015808)；标注点偏移 5787648 落在 k_decayed 段内。可与 `flash_kda_C.get_workspace_size(4096, 32, 4) == 115015808` 对拍（**待本地验证**）。

## 6. 本讲小结

- workspace 是 K1→K2 的唯一通道：每 tile 13824 字节，切成 k_decayed / q_decayed / k_restored（各 [16,128] bf16）、g_total（[128] fp32）、INV / Mqk（各 [16,16] bf16）六个分离数组，段大小全部 128 字节对齐。
- 两侧共用地址公式 \( \text{ws\_idx} = \text{head} \times \text{total\_tiles} + \text{tile} \)：K1 写侧 tile 即 `global_tile_idx`；K2 读侧 tile = `tile_base + t`，varlen 下 `tile_base` 的线性扫描与 K1 的 tile_prefix 二分给出同一编号。
- K1 由线程 0 一次性发起六次 TMA store，前置 `fence_view_async_shared` 打通 generic→async 代理可见性，后置 `tma_store_arrive` + `tma_store_wait<0>` 收尾；K2 的 LOAD warp 在 3 级流水线的每个 stage 用同一个事务 barrier 聚合发起 8 份拷贝（v、beta + 六个 workspace 分量，共 17984 字节）。
- 位一致契约：host 侧用同一批 `m_ws_*` gmem 张量、同一套 TMA smem 布局类型构造 K1 的 STORE 与 K2 的 LOAD 描述符，K1 写下的比特即 K2 读到的比特，中间零转换。
- `SharedStorageK1` 用三个 union 复用不重叠的生命周期（Phase A: q/k/g ↔ Phase B: decay 产物；g_bf16↔k_restored；dt_bias↔g_total），主 union 省下 14336 B ≈ 14 KB；安全性由 decay_apply 的「先读进寄存器 → `__syncthreads` → 再写对侧」两段式保证。
- `__launch_bounds__(256, 8)` 把 K1 压到每线程 32 个寄存器、换 8 CTA/SM 的满线程占用——单发、无流水线的 K1 只能靠并发 CTA 隐藏延迟。

## 7. 下一步学习建议

本讲把 K1→K2 的数据面（workspace 契约）讲完了。接下来进入单元三，深入 Kernel 2 内部：

- **u3-l2（Kernel 2 架构）**：本讲多次提到的 192 线程 = 4 MMA + 1 LOAD + 1 STORE 的 warp 专用化、`InputStages=3 / OutputStages=2` 多级缓冲与两条 pipeline 的完整细节；
- **u3-l3（LOAD warp）**：本讲 4.2 的延伸——事务字节预算、producer 生命周期与尾块处理；
- **u3-l1（16x16 求逆）**：本讲只引用了 `inv_fwd_subst_fused_1warp` 的产物 INV，那里有它内部的分块前代换算法；
- 复习建议：若对 `tile_to_shape` / swizzle 布局的类型推导还不熟，回到 u2-l4；对 K1 的计算主体（decay 家族如何算出来）不熟，回到 u2-l7。
