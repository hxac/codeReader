# 残余 kernel：FP16 残余 + 新 token 追加 + 原位再量化

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 residual kernel（`flash_fwd_residual_kernel` → `compute_attn_1rowblock_residualkv`）与 splitkv kernel 在 grid 划分、数据格式、输出槽位上的三点差异。
2. 在源码中精确定位「残余攒满一个 `residual_block_size` 时，kernel 内直接调用第四单元的量化原语」的那几行代码，并说明它们为什么能复用 QPack 的寄存器级原语。
3. 完整描述 `fwd_kvcache_int` 返回的 4 个 `*_new` 张量如何被 Python 侧（`llama.py` 的 `LlamaBitDecoding` 与改造版 `DynamicCache`）消费：拼接进主缓存、清空残余区。
4. 亲手跟踪一次「残余区攒满」的全生命周期，并在 `evaluation/test.py` 中用行号验证每一步。

本讲是第五单元的第四讲，承接 u5-l2（split-KV 主循环）与 u2-l2（残余机制的数据结构），把两者拼成一条完整的「FP16 残余 → 注意力 → 原位再量化 → 回写主缓存」闭环。

## 2. 前置知识

- **残余（residual）区**：KV cache 中最近若干 token 的 FP16 副本。它存在的理由有二：量化误差会被近端 token 的高注意力权重放大；新 token 若单独量化，组内样本不足、scale 不稳定。因此新 token 先进 FP16 残余区，攒满一个 `residual_block_size`（4-bit 为 128、2-bit 为 256）再统一量化（详见 u2-l2）。
- **`new_lens` 与 `seqlen_k_cache`**：`params.new_lens` 是残余区当前有效长度（1 到 `residual_block_size`），`binfo.seqlen_k_cache` 是已量化打包的主缓存 token 数。两者之和才是本次注意力的全部 KV。残余区在 Python 侧会被**补零对齐**成固定形状 `(b, residual_block_size, h_k, d)` 再传入 kernel，因此 kernel 必须用 `new_lens` 做掩码，否则零填充会以权重 \(e^{q\cdot 0}\) 混入 softmax。
- **split 槽位与 LSE 合并**：u3-l3 讲过 `set_params_splitkv` 会把启发式算出的 split 数 **+1**，多出的最后一个累积槽位正是留给 residual kernel 的；combine kernel（u5-l5）按 \(e^{lse_i - L}\) 权重合并所有 `num_splits` 个槽位。本讲会看到这个「+1」在代码里如何兑现。
- **寄存器级量化原语**：第四单元的 `qpack_Kchannel_Vtensor` / `pack_Kchannel_store` / `pack_Vtensor_store` 都操作 CuTe 寄存器 fragment（`Tensor` 对象），并不关心数据是从 gmem 加载来的（QPack kernel）还是本来就在寄存器里（本讲的 residual kernel）——这正是能「原位」复用的关键。
- **`Split` 模板参数**：为 true 时 kernel 不写最终输出 `o_ptr`，而是把未归一化的 float 部分结果写进 `oaccum_ptr` / `softmax_lseaccum_ptr`，交给 combine kernel 收尾。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [csrc/bit_decode/src/flash_fwd_kernel.h](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h) | `compute_attn_1rowblock_residualkv`（L66-556）与两个设备级包装 `compute_attn_residualkv` / `compute_attn_splitkv`（L1671-1695） |
| [csrc/bit_decode/src/flash_fwd_launch_template.h](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_launch_template.h) | `run_flash_splitkv_fwd`：一次 decode 依次启动 residual → splitkv → combine 三个 kernel，grid 划分在此 |
| [csrc/bit_decode/src/include/qpack.h](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/qpack.h) | 第四单元的量化/落盘原语，本讲在 kernel 内直接调用 |
| [csrc/bit_decode/src/include/kernel_traits.h](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/kernel_traits.h) | `residual_block_size`、`kBlockN_residual`、`SharedStorage_residual` 等残余侧编译期常量 |
| [evaluation/llama.py](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py) | `LlamaBitDecoding.forward` 的 decode 分支：补零对齐、调用 `fwd_kvcache_int`、消费 `*_new` 回写 |
| [evaluation/test.py](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py) | 不加载模型的 kernel 级验证脚本，本讲综合实践的实验台 |
| [bit_decode/models/cache_utils.py](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/models/cache_utils.py) | 改造版 `DynamicCache` 的 `update_residual` / `update_pack` / `clear_residual` |

---

## 4. 核心概念与源码讲解

### 4.1 模块一：residual kernel 的定位、grid 划分与 FP16 残余注意力

#### 4.1.1 概念说明

decode 每一步的注意力被拆给两类 kernel：

- **splitkv kernel**（u5-l2）：处理**已量化打包的主缓存**，输入是 uint16 的 `k_pack/v_pack` 加 fp32 参数，需要 LOP3 反量化才能进 Tensor Core。
- **residual kernel**（本讲）：处理 **FP16 的残余区 + 本步新追加的 token**，输入是纯 FP16，直接走标准 MMA，保证最新 token 的精度。

两者都以 `Split=true` 运行，各自把 float 部分结果写进 `oaccum/lse_accum` 缓冲的**不同 split 槽位**，最后由 combine kernel 合并。这样，「残余区攒满时顺手把这块量化掉」就发生在 residual kernel 内部——不需要任何额外的 kernel 启动，也不需要把残余区从显存重新读一遍。

#### 4.1.2 核心流程

一次 `fwd_kvcache_int` 调用在 C++ 侧启动三个 kernel（同一条 stream，串行执行）：

```
run_flash_splitkv_fwd:
  ├─ ① flash_fwd_residual_kernel   grid = (num_m_block, b, h)          写 split 槽位 num_splits-1
  ├─ ② flash_fwd_splitkv_kernel    grid = (num_m_block, num_splits-1, b*h)  写 split 槽位 0..num_splits-2
  └─ ③ flash_fwd_splitkv_combine_kernel  grid = (ceil(b*h*seqlen_q/kBlockM),)  合并全部 num_splits 个槽位
```

residual kernel 每个线程块的工作（对一对 `(bidb, bidh)`）：

1. 读 Q tile（`kBlockM × kHeadDim`，decode 时 `kBlockM=16` 而 `seqlen_q=1`，实际只有 1 行有效）。
2. 把 FP16 残余 K/V（补零对齐到 `kBlockN_residual = residual_block_size` 行）用 cp.async 装入共享内存。
3. `Q·Kᵀ` → 以 `new_lens` 为边界掩码 → 在线 softmax → `P·V`。
4. 若 `params.new_lens == residual_block_size`：在 kernel 内对本块 K/V 再量化并写出 `*_new`（模块二）。
5. epilogue：`normalize_softmax_lse` 得到 LSE，按 `n_split_idx = num_splits-1` 写入 float 累积缓冲。

#### 4.1.3 源码精读

**① grid 划分差异——三个 kernel 的对比**

[flash_fwd_launch_template.h:82-96](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_launch_template.h#L82-L96)：`num_splits_ = params.num_splits - 1`（splitkv kernel 实际可用的 split 数），residual kernel 的 grid 是 `(num_m_block, b, h)`，且共享内存超 48KiB 时用 `cudaFuncSetAttribute` 抬限。

[flash_fwd_launch_template.h:98-104](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_launch_template.h#L98-L104)：splitkv kernel 的 grid 是 `(num_m_block, num_splits_, b * h)`——注意 z 维把 batch 和 head **折叠**成一个一维下标，y 维才是 split 索引；residual kernel 则没有 split 维，z 维直接用 `(b, h)` 二维。这就是第一个差异：**residual kernel 每个 `(b, h)` 只有一个块、独占处理整个残余区；splitkv kernel 每个 `(b, h)` 有 `num_splits-1` 个块分摊主缓存**。

[flash_fwd_kernel.h:1671-1681](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1671-L1681)：`compute_attn_residualkv` 包装函数把 `blockIdx.y/z` 解包成 `bidb/bidh`，并写死 `n_split_idx = params.num_splits - 1`、`num_n_splits = 1`——**residual kernel 永远占用最后一个累积槽位**，这正是 u3-l3 中「num_splits 额外 +1」的兑现处。对照 [flash_fwd_kernel.h:1685-1695](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1685-L1695)，splitkv 包装从 `blockIdx.z / params.h` 反解 batch、`blockIdx.y` 取 split 索引。

**② 残余侧编译期常量**

[kernel_traits.h:75](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/kernel_traits.h#L75) 与 [kernel_traits.h:84-88](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/kernel_traits.h#L84-L88)：`residual_block_size = num_bits == 4 ? 128 : 256`，`kBlockN_residual = kBlockN_pack`（即残余 tile 恰好一个块装下），`kBlockP_new_pack = kBlockN_pack / pack_num`（k-channel 模式下一个块打包后的 uint16 行数，4-bit 与 2-bit 都是 32），`kBlockK_params_new = kBlockN_pack / group_size`（一个块的量化组数）。残余 kernel 专用共享内存结构 `SharedStorage_residual` 与大小 `kSmemSize_res` 定义在 [kernel_traits.h:299-308](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/kernel_traits.h#L299-L308)。

**③ 输入输出张量与「+0」偏移**

[flash_fwd_kernel.h:132-147](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L132-L147)：`residual_len = params.new_lens`，`n_blocks_residual = ceil_div(residual_len, kBlockN_residual)`——由于 `new_lens ≤ residual_block_size = kBlockN_residual`，它恒为 1。残余 K/V 的行偏移落在 `(n_blocks_residual - 1) * kBlockN_residual`（即第 0 行）；而 `*_new` 输出的行偏移处是字面量 `+ 0`，因为输出缓冲恰好只装一个块（形状见 4.3.3 的 llama.py 分配代码）。

[flash_fwd_kernel.h:149-167](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L149-L167)：构造 6 个 gmem 张量——FP16 的 `gK_residual/gV_residual`，以及打包输出的 `gK_new_pack/gK_new_params/gV_new_pack/gV_new_params`（后两者按 `__half2` 视图访问参数区，与 u4-l3 的「params 名义 fp32、实按 half2 读写」一致）。

**④ 共享内存的分时复用**

[flash_fwd_kernel.h:184-197](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L184-L197)：`sK_residual` 与 `sK_new_pack` **共用** `shared_storage.smem_Kpack` 这块存储，只是以不同布局（`SmemLayoutKResidual` / `SmemLayoutKNewPack`）解释——FP16 残余 tile 先用于注意力计算，攒满时同一块 smem 又被打包数据「寄存器→smem→gmem」的中转复用。`sV_residual/sV_new_pack` 同理共用 `smem_Vpack`。

**⑤ 主循环：加载 → QK → 掩码 → softmax → PV**

[flash_fwd_kernel.h:358-360](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L358-L360)：掩码对象以 `params.new_lens` 为序列长度构造——补零区会被掩掉。

[flash_fwd_kernel.h:366-375](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L366-L375)：先 `clear(tKsK_residual)` 预清零，再以 `params.new_lens - n_block_r * kBlockN_residual` 为边界 cp.async 装载 K。

[flash_fwd_kernel.h:378-393](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L378-L393)：注释明言「Current Residual loops only one step」——循环体只执行一次；V 的装载显式带 `Clear_OOB_MN=true`，把 `new_lens` 之外的填充位置零。

[flash_fwd_kernel.h:396-399](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L396-L399)：`flash::gemm_residual` 做 \(S = QK^\top\)，用的是 FP16 的 `TiledMma_residual`（[flash_fwd_kernel.h:258-268](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L258-L268)），完全不需要反量化——这就是「残余区保精度」在指令层面的样子。

[flash_fwd_kernel.h:423-437](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L423-L437)：`apply_mask` 掩掉填充，随后 `softmax_rescale_o</*Is_first=*/true, ...>`（唯一的迭代自然是 first）基于 exp2 做在线 softmax，维护 `row_max/row_sum/acc_o`（u5-l2 讲过同一套状态机）。

[flash_fwd_kernel.h:440-464](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L440-L464)：`acc_s` 转 FP16 经 `sAcc_residual` 中转进寄存器 fragment，再 `gemm_residual` 完成 \(O += P V\)。

**⑥ epilogue：写最后一个 split 槽位**

[flash_fwd_kernel.h:478-515](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L478-L515)：`normalize_softmax_lse</*Is_dropout=*/false, /*Split=*/true>` 归一化并产出 LSE；`row_offset_oaccum` 里出现 `n_split_idx`（对 residual kernel 即 `num_splits-1`），把 float 部分输出与 LSE 写进 `oaccum_ptr/softmax_lseaccum_ptr` 的**最后一个槽位**。combine kernel（[flash_fwd_kernel.h:1835-1853](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1835-L1853)）遍历 `split = 0..params.num_splits`，残余槽位因此被一并合并。

#### 4.1.4 代码实践

**实践目标**：用纸笔推导两个 kernel 的 grid 规模，直观感受「+1 槽位」协议。

**操作步骤**（源码阅读型，无需 GPU）：

1. 取配置：`b=2, h=32, seqlen_q=1, seqlen_k_cache=1024, kBlockM=16`，假设启发式给出 `num_splits=5`。
2. 计算 `num_m_block = ceil(seqlen_q/kBlockM)`，写出 `grid_res` 与 `grid` 两个三元组，数一数两个 kernel 各启动多少个线程块（每块 `kNThreads=128` 线程）。
3. 对照 [flash_fwd_launch_template.h:85-86](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_launch_template.h#L85-L86) 与 [flash_fwd_launch_template.h:106-108](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_launch_template.h#L106-L108)，确认 combine kernel 的 grid 只按 `b*h*seqlen_q` 切、与 split 数无关。
4. 指出 residual kernel 写的是哪个 `n_split_idx`、splitkv kernel 写的是哪些。

**需要观察的现象 / 预期结果**：`num_m_block = 1`；`grid_res = (1, 2, 32)` 共 64 块；`grid = (1, 4, 64)` 共 256 块（`num_splits_-1 = 4` 个 split × 64 个 `(b,h)` 对）。residual 写槽位 4，splitkv 写槽位 0-3，combine 合并 0-4。真实 `num_splits` 由 u3-l3 的启发式在运行时决定，此处 5 仅为示例（待本地验证：可在 `decode_api.cpp` 的 dispatch 处打印 `params.num_splits` 核对）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 residual kernel 的 grid 不需要 split 维，而 splitkv kernel 需要？

**答案**：残余区长度至多 `residual_block_size`（128 或 256），恰好等于一个 tile `kBlockN_residual`，一个线程块一次迭代就能算完，没有可切分性；而主缓存可达数万 token，必须沿序列切成多份才能喂饱 SM。二者数据量相差两个数量级，划分策略自然不同。

**练习 2**：`n_blocks_residual` 在什么情况下会大于 1？当前代码允许吗？

**答案**：当 `new_lens > kBlockN_residual` 时。由于 kernel_traits 里 `kBlockN_residual = kBlockN_pack = residual_block_size`，而 Python 侧保证 `cur_residual_len ≤ residual_block_size`（攒满即清空），所以恒为 1；循环体 `for (int residual_steps = 0; n_block_r >= 0; --n_block_r, ...)` 只跑一次，源码注释「Current Residual loops only one step」也明说了这一点，`residual_steps > 0` 分支是留空的 `// TODO`。

**练习 3**：如果去掉 `mask_residual` 的掩码（把 `new_lens` 当作 `kBlockN_residual`），输出会怎样出错？

**答案**：补零对齐产生的 K 全零行会得到分数 \(s_j = q \cdot 0 = 0\)，经 softmax 得到非零权重 \(e^{0}=1\)（归一化后约为 \(1/(L+\text{pad})\)），V 的零行又被这些权重加权求和进输出——相当于向输出注入了一个向零收缩的偏差项，同时 LSE 也被污染。掩码把这些位置设为 \(-\infty\)，权重严格为 0。

---

### 4.2 模块二：kernel 内原位再量化——qpack/quant 调用与 pack 落盘

#### 4.2.1 概念说明

残余区攒满时，这块 FP16 K/V 需要变成与主缓存同构的 `k_pack_new/k_params_new/v_pack_new/v_params_new`。朴素做法是再启动一个 QPack kernel：把残余区从显存读回来、量化、写回。BitDecoding 的做法是**在 residual kernel 内部顺带完成**——K/V 的 FP16 fragment 本来就在寄存器里喂 MMA，直接把同一份寄存器内容交给第四单元的量化原语：

- 量化输入零额外显存读：复用 `tSrK_residual`（刚喂过 Q·Kᵀ）与 `tOrVt_residual`（刚喂过 P·V）。
- 零额外 kernel 启动：量化与落盘内联在注意力 kernel 中，只在「攒满」的那一步才真正执行。
- 原语零改动：`qpack_Kchannel_Vtensor` 等函数操作的是 CuTe 寄存器 Tensor，与 QPack kernel 中「gmem→smem→寄存器」来源的数据用法完全一致。

这就是「原位（in-place / piggyback）再量化」的含义。

#### 4.2.2 核心流程

触发条件是运行期标量比较 `params.new_lens == residual_block_size`（`new_lens` 由 Python 侧的 `cur_residual_len` 传入）：

```
K 侧（QK gemm 之后、掩码/softmax 之前）:
  if new_lens == residual_block_size:
      quant_mode == 1 (k-channel):
          qpack_Kchannel_Vtensor<num_bits>(tSrK_residual → tSrK_new_pack, tScales_k_cr, tZeros_k_cr)   # 寄存器内量化
          pack_Kchannel_store(...)     # 打包位: 寄存器→smem→gmem(k_pack_new); scale/zero: 寄存器→gmem(k_params_new)
      else (k-tensor, 当前仓库 dispatch 未启用):
          quant_Ktensor(...) + pack_Ktensor_store(...)

V 侧（PV gemm 之后）:
  if new_lens == residual_block_size:
      qpack_Kchannel_Vtensor<num_bits>(tOrVt_residual → tOrVt_new_pack, tScales_v_tr, tZeros_v_tr)     # V 恒为 tensor 布局
      pack_Vtensor_store<num_bits, kHeadDim>(...)
```

量化数学与第四单元完全相同（组内 max/min → `zero=min, scale=range/max_val` → `q=clip(round((x-zero)·scale_inv))` → 压入 uint16），此处不重复。

#### 4.2.3 源码精读

**① K 的再量化触发点**

[flash_fwd_kernel.h:401-420](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L401-L420)：紧跟 `flash::gemm_residual`（Q·Kᵀ）之后。`quant_mode == 1` 分支调用 `quant::qpack_Kchannel_Vtensor<num_bits>(tSrK_residual, tSrK_new_pack, tScales_k_cr, tZeros_k_cr, sReduce_tmp, num_params)`——注意第一个参数正是刚才喂给 MMA 的 **FP16 K fragment**，第三个输出是打包后的 **uint16 fragment**，`sReduce_tmp` 是复用 `smem_acc` 存储的归约缓冲（u4-l2 的 warp/allreduce 原语在此工作）。随后 `quant::pack_Kchannel_store(...)` 负责落盘。`else` 分支的 `quant_Ktensor/pack_Ktensor_store` 是 k-tensor 路线，当前仓库 dispatch 层未启用（u3-l1 讲过 if 链只放行 k-channel × group_size∈{32,128}）。

**② V 的再量化触发点**

[flash_fwd_kernel.h:466-475](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L466-L475)：放在 PV gemm **之后**，量化对象是 `tOrVt_residual`（V 的转置 fragment）。V 不分 quant_mode，恒为 tensor 布局，所以直接调 `qpack_Kchannel_Vtensor` + `pack_Vtensor_store<num_bits, kHeadDim>`（与 u4-l3 讲的「K 逐通道 / V 逐张量共用同一量化器」一致）。

**③ 被调用的量化器与落盘函数**

[qpack.h:302-310](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/qpack.h#L302-L310)：`qpack_Kchannel_Vtensor` 只是转发到 `qpack_kc_vt<num_bits>::apply`——第四单元精读过的按 `num_bits` 特化的量化器。

[qpack.h:499-525](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/qpack.h#L499-L525)：`pack_Kchannel_store` 三段式——`cute::copy` 寄存器→smem（L508）、`__syncthreads()` 后 smem→gmem 写 `k_pack_new`（L512）、scale/zero 直接从寄存器按 `params(j % num_params, 0 + 8*i + 4*(j/num_params) + tidx%4)` 的散布公式写 `k_params_new`（L516-523）。写读公式与 splitkv kernel 的 `load_params` 严格镜像（贯穿性不变量，u4-l3）。

[qpack.h:527-543](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/qpack.h#L527-L543)：`pack_Vtensor_store` 骨架相同，仅 2-bit/hdim128 时限制 `threadIdx.x < 64` 参与寄存器→smem 拷贝（寄存器产出数量翻倍下的权衡，见 u5-l3）。

**④ 打包输出的 R2S / S2G 拷贝管线**

[flash_fwd_kernel.h:307-326](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L307-L326)：`smem_tiled_copy_kv_pack`（基于 `R2SCopyAtomPack` 与 `TiledMmaKV_i4`）把打包 fragment 从寄存器摆进 `sK_new_pack/sV_new_pack`；`gmem_tiled_copy_kv_newpack` 再从共享内存写到 `gK_new_pack/gV_new_pack`。这条管线在函数开头一次性搭好，「攒满」时才启用。

#### 4.2.4 代码实践

**实践目标**：把「同一份寄存器数据先喂 MMA、再喂量化器」的证据链找全。

**操作步骤**（源码阅读型）：

1. 在 [flash_fwd_kernel.h:266-276](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L266-L276) 找到 `tSrK_residual` 的诞生地（`thr_mma_residual.partition_fragment_B(sK_residual)`，L268）。
2. 追踪它被使用的两处：L397（`gemm_residual` 的 B 操作数）与 L403（`qpack_Kchannel_Vtensor` 的 `src`）。
3. 对 `tOrVt_residual` 做同样的事（诞生于 L273，使用于 L460 与 L468）；顺带确认 [flash_fwd_kernel.h:290-301](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L290-L301) 里 `retile_D` 生成的只是配套视图。
4. 列一张表：`寄存器张量 | 诞生行 | MMA 使用行 | 量化使用行 | 量化输出`。

**需要观察的现象 / 预期结果**：

| 寄存器张量 | 诞生 | 喂 MMA | 喂量化 | 量化输出 |
| --- | --- | --- | --- | --- |
| `tSrK_residual` | L268 | L397（Q·Kᵀ） | L403 | `tSrK_new_pack` + `tScales_k_cr/tZeros_k_cr` |
| `tOrVt_residual` | L273 | L460（P·V） | L468 | `tOrVt_new_pack` + `tScales_v_tr/tZeros_v_tr` |

两处量化调用与对应 gemm 之间**没有任何 smem/gmem 往返**——这就是「零额外显存读」的直接证据。

#### 4.2.5 小练习与答案

**练习 1**：为什么 K 的再量化放在 QK gemm 之后、softmax 之前，而 V 的放在 PV gemm 之后？

**答案**：量化必须发生在数据「已经生产出来」之后。K fragment 在装入 smem 后即可用（gemm 前后都在寄存器里），放在 gemm 后是为了让 MMA 与量化的寄存器依赖自然衔接；V fragment 同理要在 PV gemm 消费完之后才可覆盖/复用。另外 `pack_*_store` 内部有 `__syncthreads()`，插入位置还起到块内同步的作用，不打断 softmax 状态机的正确性。

**练习 2**：非攒满的普通 decode 步（`new_lens < residual_block_size`），kernel 为量化多付出了什么代价？

**答案**：几乎为零。`if (params.new_lens == residual_block_size)` 是块内一致的标量比较，不成立时整块直接跳过量化与落盘；只有函数开头为输出搭好的 R2S/S2G 拷贝对象（纯寄存器/模板元数据，不占运行时开销）和一些未使用的 `tScales/tZeros` 寄存器声明。注意力路径完全不受影响。

**练习 3**：`tScales_k_cr` 的类型是 `TensorParamsKC_residual`（形状 `(4*num_params, tile_paramsk_k)`），后缀 `_cr` 里的 c 和 r 各指什么？

**答案**：c 指 k-channel（逐通道量化），r 指 residual（残余路径专用）。它是残余路径专用的 scale/zero 寄存器摘要布局，与 QPack kernel 用的 `TensorParamsKC`（多一个 `tile_paramsk_m` 维）同族但维度更少——残余块只有一个 tile，无需沿 m 展开。

---

### 4.3 模块三：Python 侧消费——fwd_kvcache_int 返回值、update_pack 拼接与 clear_residual

#### 4.3.1 概念说明

kernel 写出的 `*_new` 四件套只是「暂存在调用方提供的缓冲里」。要不要拼进主缓存、何时清空残余区，由 **Python 侧**根据 `cur_residual_len` 决定：

- `fwd_kvcache_int` 返回 5 元组 `(out, k_pack_new, k_params_new, v_pack_new, v_params_new)`（u2-l3）。
- 只有当 `cur_residual_len == residual_block_size` 时后 4 个才有意义，此时 `LlamaBitDecoding`（或 test.py）调用 `update_pack` 把它们 `torch.cat` 进主缓存，再 `clear_residual` 清空 FP16 残余区。
- 四个 `*_new` 缓冲在 **prefill 末尾一次性分配**、挂在 attention 模块（`self.k_pack_new` 等）上，每步 decode 作为 out 参数传入又传出——避免每步分配显存。

#### 4.3.2 核心流程

`LlamaBitDecoding.forward` 的 decode 分支（`q_len == 1`）：

```
1. update_pack(None, None, None, None, layer)      # 全 None → 纯读取，取回主缓存 4 张量
2. seqlen_pack = v_pack.shape[1]；seqlens_k = full(b, seqlen_pack)   # 主缓存 token 数
3. 分配补零缓冲 k_residual/v_residual: (b, residual_block_size, h_k, d)
4. update_residual(k_new, v_new, layer)             # 追加新 token，返回完整 FP16 残余区
5. cur_residual_len = 残余区长度；把残余区拷进补零缓冲前 cur_residual_len 行
6. fwd_kvcache_int(q, 主缓存4张量, 补零残余, seqlens_k, self.*_new, ..., cur_residual_len, num_bits)
   → (attn_output, k_pack_new, k_params_new, v_pack_new, v_params_new)
7. if cur_residual_len == residual_block_size:
       update_pack(k_pack_new, k_params_new, v_pack_new, v_params_new, layer)   # 拼接主缓存
       clear_residual(layer)                                                     # 清空残余区
```

`update_pack` 的拼接维度是布局的硬证据（u2-l1）：K 系（`k_pack/k_params`）沿 `dim=-3`（序列/打包行维），`v_pack` 沿 `dim=-3`（序列维），唯独 `v_params` 沿 `dim=-1`——因为它的形状是 `(b, d/group_size, h, s)`，序列在最后一维。

#### 4.3.3 源码精读

**① decode 分支：读取、补零、调用、条件回写**

[llama.py:648-663](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L648-L663)：`update_pack(None,...)` 读取主缓存；`torch.zeros((batch_size, self.residual_block_size, nheads_k, d))` 分配补零缓冲；`update_residual` 追加新 token；`k_residual[:, :cur_residual_len] = k_residual_cache` 完成对齐拷贝。

[llama.py:666-679](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L666-L679)：`fwd_kvcache_int` 的 5 元组解包——四个 `self.*_new` 既是输入（第 10-13 个实参，out 缓冲）又是输出（返回值），`cur_residual_len` 以 `new_lens` 之名传入 kernel，决定掩码边界与再量化触发。

[llama.py:681-683](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L681-L683)：触发条件与两步回写——`update_pack(self.k_pack_new, ...)` + `clear_residual(self.layer_idx)`。

[llama.py:747-750](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L747-L750)：prefill 分支末尾一次性分配四个 `*_new` 缓冲，形状与主缓存同构、序列维换成一个 `residual_block_size`：k-channel 下 `k_pack_new = (b, residual_block_size/pack_nums, h_k, d)`、`k_params_new = (b, residual_block_size/group_size, h_k, d)`、`v_pack_new = (b, residual_block_size, h_k, d/pack_nums)`、`v_params_new = (b, d/group_size, h_k, residual_block_size)`。这与 kernel 侧 `gK_new_pack/gV_new_pack` 的形状（4.1.3 ③）逐一对上。

**② 缓存三方法**

[cache_utils.py:559-605](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/models/cache_utils.py#L559-L605)：`update_residual` 沿 `dim=-3` 追加（L602-603），残余区复用 `key_cache/value_cache` 字段（u2-l2）。

[cache_utils.py:607-662](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/models/cache_utils.py#L607-L662)：`update_pack` 四路 `torch.cat`（L657-660）——前三路 `dim=-3`、`v_params` 一路 `dim=-1`，拼完 `.contiguous()`；传全 None 时跳过更新、纯读取（L633 的 `if key_pack is not None`）。

[cache_utils.py:664-666](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/models/cache_utils.py#L664-L666)：`clear_residual` 把该层的 `key_cache/value_cache` 置回空列表，残余长度归零。

**③ test.py 中的同构消费**

[test.py:137-154](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L137-L154)：与 llama.py 逐行同构——`fwd_kvcache_int` 返回 5 元组（L137），`if cur_residual_len == residual_block_size:` 触发 `update_pack` + `clear_residual`（L152-154）。缓冲分配在循环前（[test.py:110-113](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L110-L113)）。

#### 4.3.4 代码实践

**实践目标**：跟踪一次「残余区攒满」的完整生命周期，写出按时序排列的 8 步说明，并在 `evaluation/test.py` 中找到对应代码行验证。

**操作步骤**（需 GPU；无 GPU 时完成步骤 1-2 的推导部分并标注「待本地验证」）：

1. **先做笔算**：把 [test.py:57](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L57) 的 `seqlen_k` 从 1024 改为 1000（`residual_block_size=128`），则初始残余 \(1000 \bmod 128 = 104\)，decode 循环每轮 +1，推出第几轮 `cur_residual_len` 首次等于 128。
2. 把 [test.py:116](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L116) 的 `range(32)` 保持不变（104+32=136 > 128，足以触发），运行 `python evaluation/test.py`。
3. 观察每轮打印的 `cur_residual_len`（[test.py:132](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L132)）与 MAE：触发轮之后 `cur_residual_len` 应回落到 1，且**触发前后的 MAE 不应有跳变式恶化**（残余区量化回主缓存引入的误差与 prefill 量化同源）。

**需要观察的现象 / 预期结果**：`cur_residual_len` 依次打印 105, 106, …, 128（第 24 轮，0 基 round_idx=23），该轮触 `update_pack+clear_residual`，下一轮打印 1。若把 `seqlen_k` 保持 1024（初始残余 0），则 32 轮内 `cur_residual_len` 最大为 32，永不触发——这正是 u1-l4 的结论。（运行结果待本地验证。）

**8 步时序说明**（括号内为 test.py 对应行号；模型侧对应 llama.py）：

1. decode 前向生成新 token 的 `k_new/v_new`，形状 `(b, 1, h_k, d)`（L117-118）。
2. `past_key_value.update_residual(k_new, v_new, layer_idx)` 沿 `dim=-3` 追加进 FP16 残余缓存，返回完整残余区（L129；cache_utils.py L602-603）。
3. 残余区拷入补零对齐缓冲 `(b, 128, h_k, d)`，记录 `cur_residual_len`（L127-135）。
4. `fwd_kvcache_int(...)` 进入 C++，`run_flash_splitkv_fwd` 依次启动 residual / splitkv / combine 三个 kernel（L137-150）。
5. residual kernel 内 FP16 路径算残余区注意力；因 `new_lens == residual_block_size`，QK gemm 后对 `tSrK_residual` 调 `qpack_Kchannel_Vtensor + pack_Kchannel_store` 写出 `k_pack_new/k_params_new`，PV gemm 后同理写出 `v_pack_new/v_params_new`（flash_fwd_kernel.h L401-420、L466-475）。
6. `fwd_kvcache_int` 返回 5 元组，`k_pack_new` 等四件套被解包（L137）。
7. `cur_residual_len == residual_block_size` 成立 → `update_pack(k_pack_new, ...)` 把四件套按 `dim=-3`（`v_params` 为 `dim=-1`）拼进主缓存（L152-153；cache_utils.py L657-660）。
8. `clear_residual(layer_idx)` 置空 FP16 残余区，下一轮从 0 重新积累（L154；cache_utils.py L664-666）。

#### 4.3.5 小练习与答案

**练习 1**：如果删掉 llama.py L681-683 的 `if` 块（即永远不回写、不清空），会发生什么？

**答案**：`cur_residual_len` 会超过 `residual_block_size`，而补零缓冲形状固定为 `(b, residual_block_size, h_k, d)`，`k_residual[:, :cur_residual_len]` 切片越界直接报错；即便缓冲够大，kernel 侧 `kBlockN_residual` 也只有一个 tile，且 `n_blocks_residual` 变成 2 后走的是空的 `// TODO` 分支。回写-清空协议是整个布局的刚性约束。

**练习 2**：为什么 `self.k_pack_new` 等缓冲挂在 attention 模块上跨步复用，而不是每步 `torch.empty`？

**答案**：复用同一块显存可以 (a) 省去每步的分配/释放（即便有 caching allocator 也有开销与碎片风险）；(b) 作为 out 参数传给 kernel 时指针稳定，便于潜在的 CUDA Graph 捕获；(c) 语义上「上一轮的输出缓冲本轮被覆盖」天然成立，因为只在攒满轮才被真正写入。

**练习 3**：`update_pack` 里为什么 `value_cache_params` 用 `dim=-1` 而其他三个用 `dim=-3`？

**答案**：由 u2-l1 的布局决定：`k_pack (b, s/pack, h, d)`、`k_params (b, s/g, h, d)`、`v_pack (b, s, h, d/pack)` 的序列（或打包行）都在 `dim=-3`；而 `v_params` 形状为 `(b, d/g, h, s)`，序列维被特意放到最后一维，使同一量化组的参数在内存中连续、kernel 加载 `v_params` 时合并访存友好（u3-l2 讲过这一非对称 stride 的动机）。

---

## 5. 综合实践

**任务：把「攒满-回写」事件变成可见的阶梯，并解释它。**

在有 GPU 的机器上：

1. 复制 `evaluation/test.py` 为 `test_residual_trace.py`（放在同一目录运行，不要改动源文件）。
2. 设 `seqlen_k = 1000`、`residual_block_size = 128`、decode 轮数改为 `range(40)`。
3. 在循环内加两行打印：`v_pack.shape[1]`（主缓存 token 数，由 `update_pack(None,...)` 返回的 `v_pack` 得到）与 `cur_residual_len`。
4. 再把 `num_bits` 改为 2（注意 `residual_block_size` 相应变为 256，初始残余 \(1000 \bmod 256 = 232\)，触发轮相应推迟），重跑一次。
5. 画出/列出具两张表：`轮次 → cur_residual_len` 与 `轮次 → 主缓存 token 数`，圈出触发轮。

预期你会看到：`cur_residual_len` 锯齿上升、到 128（或 256）瞬间归 1；主缓存 token 数是一条阶梯线，在触发轮一次跳升 128（或 256）。这就是「FP16 残余 + 攒满再量化」在宏观上的节律，也是 llama.py 中被注释掉的调试打印（[llama.py:685-687](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L685-L687)）想观察的现象。无 GPU 时写出完整推导表并标注「待本地验证」。

## 6. 本讲小结

- decode 的注意力被拆给 residual kernel（FP16 残余 + 新 token，grid `(m, b, h)`，独占最后一个 split 槽位 `num_splits-1`）与 splitkv kernel（量化主缓存，grid `(m, num_splits-1, b*h)`），由 combine kernel 按 LSE 合并——u3-l3 的「+1 槽位」在此兑现。
- residual kernel 的主循环只跑一个 tile（`kBlockN_residual = residual_block_size`），以 `new_lens` 掩掉补零区，全程 FP16 MMA，保住最新 token 的精度。
- 当 `params.new_lens == residual_block_size` 时，kernel **原位**复用第四单元的 `qpack_Kchannel_Vtensor / pack_Kchannel_store / pack_Vtensor_store`，把刚喂过 MMA 的 K/V 寄存器 fragment 量化打包写出 `*_new` 四件套——零额外 kernel 启动、零额外显存读。
- Python 侧由 `cur_residual_len == residual_block_size` 触发 `update_pack`（K 系 `dim=-3`、`v_params` `dim=-1` 拼接）+ `clear_residual`，闭环完成；`*_new` 缓冲 prefill 末尾分配、跨步复用。
- test.py 与 llama.py 的消费逻辑逐行同构，前者是不加载大模型的理想实验台。

## 7. 下一步学习建议

- 下一讲（u5-l5）精读 combine kernel 的 `combine_attn_seqk_parallel`：本讲留下的「残余槽位 + 各 split 槽位」如何被 \(O = \sum_i e^{lse_i - L} O_i\) 合并成最终输出，届时可回头验证本讲的槽位编号。
- 若想巩固量化原语细节，回读 u4-l2/u4-l3 的归约与落盘，再对照本讲 4.2.3 的调用点，体会「同一原语、两种宿主 kernel」的设计。
- 若关心模型集成视角，u6-l2 会把本讲的 `LlamaBitDecoding` decode 分支放进完整的 prefill-decode 双路径与多层循环中讨论。
