# u2-l7 Kernel 1 计算阶段：L2 归一化、门控 cumsum 与 decay_apply

## 1. 本讲目标

上一讲（u2-l6）我们读完了 Kernel 1（`_flash_kda_fwd_prepare`）的骨架：tile 映射、varlen 二分查找、单发 TMA 加载与事务 barrier。本讲进入它的**计算主体**——从 `__syncthreads()` 等 TMA 落地之后、到六次 TMA store 之前的那约 250 行代码。读完本讲你应该能：

1. 独立讲出 K1 计算主体的四个阶段（L2 归一化 → 门控激活+cumsum+尾清零 → decay_apply → L/Mqk 构造+掩码）各自在算什么、为什么这样排。
2. 把 decay_apply 的线程-数据映射（`warp_id/lane/g/t` ↔ 16×128 矩阵元素）翻译成 Python，并证明 256 线程完整覆盖、无重叠。
3. 对照 `tests/torch_ref.py` 逐条验证 kernel 每一步的数值行为（`ex2`/`tanh` 近似、FMA 顺序、bf16 量化点），理解「bit-exact 参考实现」到底在模拟什么。

## 2. 前置知识

本讲反复用到几个数值与硬件概念，先用一段话讲透：

- **bf16 与舍入**：bf16 有 8 位指数、7 位尾数（含隐含位），动态范围与 fp32 相同但精度只有约 3 位十进制。cutlass 的 `bfloat16_t` 算术运算（`operator*` 等）是「转 fp32 → 运算 → RNE 舍入回 bf16」，**每次运算单独舍入一次**。torch 的 bf16 逐元素乘法语义相同。所以 kernel 里 `q * exp_cumsum * BF16(scale)` 这种链式 bf16 乘法和 torch_ref 里对应的两次 bf16 乘法逐位一致。
- **FMA 单次舍入**：`a + b*c` 若编译成 FMA 指令，乘积不在中间舍入，只在最后加法后舍入一次；若拆成「先乘后加」则舍入两次。bit-exact 复刻必须固定这个顺序（见 torch_ref 的 `fp32_fma`）。
- **PTX 近似指令**：`ex2.approx.ftz.f32` 是以 2 为底的快速指数（ftz = flush-to-zero，下溢清零）；`tanh.approx.f32` 是 tanh 的硬件近似。项目用 `--use_fast_math` 编译，所以源码里的普通 `expf` 也会被编译成 `ex2.approx.ftz(x * log2e)`——这就是为什么 kernel 敢直接写 `expf` 而 torch_ref 用 `fp32_ex2_ftz(A_log * LOG2E)` 对拍。
- **换底（承接 u2-l2）**：kernel 里所有门控值都在 **log2 域**。host 侧预乘 `gate_scale = lower_bound * log2(e)`，此后一切指数都用 `ex2` 完成，等价于自然域的 \( e^{g} \)。
- **warp 与 shuffle**：一个 warp 是 32 个 lane；`__shfl_xor_sync(mask, val, delta)` 让 lane \(i\) 与 lane \(i \oplus delta\) 交换寄存器值。对 delta = 8,4,2,1 各做一轮，就是一棵蝶形归约树。
- **smem union**（承接 u2-l8 预告）：K1 的 `SharedStorageK1` 用匿名 union 让 Phase A 缓冲（q/k/g）与 Phase B 缓冲（k_decayed/q_decayed/k_inv/L/INV/Mqk）复用同一块共享内存。**凡是从 union 一侧的缓冲读、写另一侧的缓冲，中间必须有 `__syncthreads()`**——本讲 decay_apply 的两段式循环就是为这个。

如果对 KDA 的门控数学（\( \tilde g = \ell \cdot \sigma(e^{a}(g_{raw}+b)) \)）或四个衰减变体（k_decayed/k_inv/q_decayed/k_restored）不熟悉，请先回看 u1-l2 与 u2-l1。

## 3. 本讲源码地图

| 文件 | 本讲关注范围 | 作用 |
|---|---|---|
| [csrc/smxx/fwd_kernel1.cuh](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh) | L257-L507 | K1 计算主体：L2 归一化、门控 cumsum、decay_apply、L/Mqk 构造 |
| [csrc/smxx/utils.cuh](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh) | L38-L59, L145-L192 | 近似指令包装（ex2/tanh/sigmoid/bf16 转换）与两个单 warp MMA 帮手函数 |
| [tests/torch_ref.py](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py) | L44-L78, L161-L225 | bit-exact 参考实现：数值工具箱、门控激活、chunk 循环内的衰减变体与 tril 掩码 |
| [csrc/smxx/fwd_launch.cu](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu) | L146-L179 | K1 启动配置：`kK1Threads = 256`、grid `(total_tiles, H)` |

K1 计算主体的整体流水（本讲覆盖 1-4 步，5-6 步只做衔接）：

```text
TMA 数据落地（u2-l6）
   │
   ├─ ① QK L2 归一化（原地写回 q/k smem）            L265-L304
   ├─ ② 门控激活 + inclusive cumsum ∥ k 尾行清零       L306-L338
   ├─ ③ g_total 就地变换为 exp(g_total)               L353-L358
   ├─ ④ decay_apply：读进寄存器 → sync → 写出四个变体  L360-L475
   ├─ ⑤ 双单 warp MMA 构造 L(fp32) 与 Mqk(bf16) + tril/beta 掩码
   │                                                    L478-L507
   ├─ ⑥ 16×16 求逆 inv_fwd_subst_fused_1warp           L510-L511（u3-l1）
   └─ ⑦ 六次 TMA store 写 workspace                    L515-L584（u2-l8）
```

## 4. 核心概念与源码讲解

### 4.1 QK 的 L2 归一化：每线程 8 元素 + 16-lane 蝶形归约

#### 4.1.1 概念说明

u1-l2 已建立：q/k 必须做 L2 归一化，这是 delta 规则「精确替换」性质与块内 \( I+L \) 良态的前提；q 另乘 `scale`（= \( 1/\sqrt{D} \)），v/g 不归一化。归一化的数学定义很简单：

\[ \hat{q}_t = \frac{q_t}{\|q_t\|_2 + \epsilon}, \qquad \|q_t\|_2 = \sqrt{\textstyle\sum_{d=0}^{127} q_{t,d}^2} \]

难点不在数学，在**如何用 256 个线程并行处理 16 行 × 128 列、且归约顺序逐位确定**——因为 torch_ref 要 bit-exact 复刻这个顺序。kernel 的答案是「**行内 16 线程分段求和 + 蝶形树归约**」，即把每行的 128 个元素按线程切成 16 段、每段 8 个：线程先在寄存器里把 8 个元素的平方和**按 i 升序 FMA 累加**，再用 4 轮 xor-shuffle 把 16 个部分和归约成一个。

#### 4.1.2 核心流程

```text
ELEMS_PER_THREAD = 8, THREADS_PER_ROW = D/8 = 16   # 每行 16 线程，一个 warp 恰好管 2 行
my_row  = tid / 16        # 0..15（行 = chunk 内 token 序号）
my_col  = (tid % 16) * 8  # 每线程负责 8 个连续列

for i in 0..7:                     # 顺序 FMA（单次舍入）
    q_sq = fma(q[i], q[i], q_sq)   # 同 k_sq
for delta in [8,4,2,1]:            # 16-lane 蝶形归约（不跨 16-lane 组）
    q_sq += shuffle_xor(q_sq, delta)
q_inv = rsqrt(q_sq + 1e-6)         # 平方根与除法合并成一条 rsqrt
原地写回 BF16(q[i] * q_inv)
```

两处细节决定了它能被 torch_ref 逐位复刻：

- delta 从 **8** 开始而不是 16：每行占 16 个连续线程（半 warp），xor 8,4,2,1 的交换永远发生在 16-lane 组内部，warp 的上下两半各自归约各自的行，互不污染。
- `+1e-6f` 防零向量（极端输入或 padding）导致 `rsqrt(0)=inf`。

#### 4.1.3 源码精读

归一化主体在 [csrc/smxx/fwd_kernel1.cuh:265-304](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L265-L304)：先按 `my_row/my_col` 把 q/k 读进 fp32 寄存器并算平方和，再做 4 轮 `__shfl_xor_sync(0xFFFFFFFF, ..., delta)` 蝶形归约，最后 `rsqrtf` 与 bf16 原地写回。bf16→fp32 的转换走 [csrc/smxx/utils.cuh:55-59](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L55-L59) 的 `bf16_to_f32`（内联 PTX `cvt.f32.bf16`）。

对照 torch_ref 的 [tests/torch_ref.py:62-78](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L62-L78) `l2_normalize_kernel_match`，三个数值关键点一一对应：

| kernel 行为 | torch_ref 模拟 |
|---|---|
| `q_sq += qv * qv`（i 升序，FMA 单次舍入） | `fp32_fma(partials, groups[..., i], groups[..., i])`，i 升序（[tests/torch_ref.py:70-71](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L70-L71)） |
| xor 蝶形 delta = 8,4,2,1 | `for offset in [8,4,2,1]: partials + partials[..., idx^offset]`（[tests/torch_ref.py:73-75](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L73-L75)） |
| `rsqrtf(q_sq + 1e-6f)`，写回时一次 bf16 舍入 | `torch.rsqrt(partials[..., 0:1] + 1e-6)` + `.to(x.dtype)`（[tests/torch_ref.py:77-78](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L77-L78)） |

其中 `fp32_fma`（[tests/torch_ref.py:55-59](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L55-L59)）用「升 fp64 精确计算、一次性舍回 fp32」来模拟硬件 FMA 的单次舍入——这是整个参考实现的基石技巧。

注意归一化是**原地写回** Phase A 的 q/k smem：后续 decay_apply、Mqk/L 的 MMA 读的都是归一化后的值；torch_ref 同样在 chunk 化之前先做全局归一化（[tests/torch_ref.py:158-159](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L158-L159)），顺序一致。

#### 4.1.4 代码实践

**实践目标**：验证「delta = 8,4,2,1 的 xor 蝶形归约」在 32-lane warp 上等价于「上下两个 16-lane 组各自求和」，且不串行。

**操作步骤**：保存以下脚本为 `butterfly_check.py`（纯 Python，无需 GPU），运行 `python butterfly_check.py`。

```python
# 示例代码：蝶形归约顺序的纯 Python 复刻
def butterfly_reduce(vals):            # vals: 长度 32，模拟一个 warp 的 partials
    for delta in (8, 4, 2, 1):
        vals = [vals[i] + vals[i ^ delta] for i in range(32)]
    return vals                        # 每个lane都持有其 16-lane 组的总和

import random
vals = [random.random() for _ in range(32)]
out = butterfly_reduce(vals[:])
assert abs(out[0]  - sum(vals[0:16]))  < 1e-12   # 下半 warp = 第 0 行
assert abs(out[15] - sum(vals[0:16]))  < 1e-12
assert abs(out[16] - sum(vals[16:32])) < 1e-12   # 上半 warp = 第 1 行
# 反例：delta 从 16 开始会跨行混合
v2 = vals[:]
for delta in (16, 8, 4, 2, 1):
    v2 = [v2[i] + v2[i ^ delta] for i in range(32)]
assert abs(v2[0] - sum(vals)) < 1e-12           # 全 warp 一个和 → 行被污染
print("OK: delta=8..1 在 16-lane 组内归约；delta=16 起步会跨行")
```

**需要观察的现象**：前两组断言通过（每个 lane 拿到自己那行的和），第三组显示 delta=16 起步时全 warp 混成一个总和。

**预期结果**：打印 `OK: ...`。若把 kernel 的循环改成从 16 开始，两个行的范数会变成同一个错误值——torch_ref 的 `l2_normalize_kernel_match` 也会随之失配。

#### 4.1.5 小练习与答案

**练习 1**：为什么每线程恰好 8 个**连续**元素，而不是跨步取数？
**答案**：8 个连续 bf16 = 16 字节，恰好是一条 128-bit 向量化访存；跨步取数则退化为标量访问。同时「16 线程/行 × 8 元素」正好铺满 D=128，不需要任何边界判断。

**练习 2**：为什么归一化结果要**原地**写回 q/k smem，而不是另开缓冲？
**答案**：Phase A 的 q/k 在归一化之后就再不需要原始值了，原地覆盖省掉 2×4KB smem；且所有后续消费者（decay_apply 的读、L/Mqk 的 LDSM）都直接指向同一缓冲，无需指针切换。

**练习 3**：`rsqrtf` 近似与 `1.0f/sqrtf` 的舍入行为相同吗？
**答案**：不同。`rsqrtf` 是单条近似指令（一次舍入），`1/sqrt` 是两条指令（两次舍入）。kernel 用 `rsqrtf`，torch_ref 对应用 `torch.rsqrt`——参考实现必须在指令粒度上对齐，这正是「bit-exact」的成本。

---

### 4.2 融合门控激活 + cumsum + k 尾部清零：一个 block、两种分工

#### 4.2.1 概念说明

这一步要把 bf16 的原始门控 logits \( g_{raw} \) 变成 log2 域的**逐行累积遗忘量**（inclusive cumsum），顺便处理 varlen 尾块。数学上（自然域）：

\[
\tilde g_{t,d} = \ell \cdot \sigma\!\big(e^{a_d}\,(g^{raw}_{t,d} + b_d)\big), \qquad
gc_{t,d} = \sum_{s \le t} \tilde g_{s,d} \cdot \log_2 e, \qquad
g_{total,d} = gc_{L-1,d}
\]

其中 \( a = e^{A\_log} \)、\( b = dt\_bias \)、\( \ell = lower\_bound \)。kernel 里 `gate_scale` 已在 host 侧预乘 \( \ell \log_2 e \)（u2-l2 讲过换底），所以存进 smem 的 `g` 与 `g_total` 都是 log2 域——后面所有 `ex2` 才能直接用。

这一步还有**两件事在同一段代码里并行发生**：

1. **线程 0-127（每线程一列）**：顺序扫 16 行做激活+cumsum。cumsum 沿行方向有顺序依赖，16 个元素做并行 scan 毫无收益，「一列一线程、寄存器里跑完」最优，还省掉了「激活结果写 smem 再读回来」的一次往返（源码注释称之为 eliminates raw-g smem round-trip）。
2. **线程 128-255（每线程一列）**：把 k 的**尾行清零**。`actual_len = min(CHUNK, seq_len - local_t*CHUNK)` 是本 tile 的真实 token 数；varlen 尾块（以及 batched 非整除时的尾 tile）中 row ≥ actual_len 的 k 行清零。

#### 4.2.2 核心流程

```text
actual_len = min(16, seq_len - local_t*16)

线程 tid < 128:                                线程 128 <= tid < 256:
  col = tid                                      col = tid - 128
  sum = 0                                        for row in [actual_len, 16):
  for row in 0..15:                                k_smem[row][col] = 0
      if row < actual_len:
          x = bf16_to_f32(g_bf16[row][col]) + dt_bias[col]
          x = a_log_exp * x
          gv = gate_scale * sigmoid_tanh(x)   # log2 域、含换底
      else:
          gv = 0.0                            # padding 行贡献 0
      sum += gv
      g_smem[row][col] = sum                 # inclusive cumsum
  g_total[col] = sum                         # 只含真实 token 的总遗忘
```

三个不变量：

- **padding 行 gv=0** → cumsum 在 padding 行冻结，`g_total` 只累计真实 token。若多加，块级状态衰减 \( 2^{g\_total} \)（K2 用它更新整块状态）会过度遗忘。
- **k 尾行清零** → 之后 decay_apply 生成的 k_decayed/k_inv/k_restored 尾行恒为 0。这是**状态更新正确性**的关键：K2 里 \( \delta s = k\_restored^\top U \)，而 U 的尾行可能携带垃圾（v 的尾行来自 eos 之后的越界读取），k_restored 尾行为 0 就把这些垃圾项精确消灭。
- **q 的尾行不清零** → 不是遗漏。q 尾行垃圾只会流入 out / Mqk 的尾行，而 K2 的尾块写出路径只写 `actual_len` 行（u3-l7），垃圾永远不被消费。torch_ref 用「整个 chunk 先清零再填 `[:actual_len]`」的写法（[tests/torch_ref.py:196-206](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L196-L206)）达到同一效果，kernel 只对 k 做是精确最小化的选择。

#### 4.2.3 源码精读

双任务分支在 [csrc/smxx/fwd_kernel1.cuh:306-338](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L306-L338)：`compute_tid < 128` 的线程做门控激活 + 顺序 cumsum 并写 `g_total`；其余线程对 k 尾行写 `BF16(0)`。激活三连（加 dt_bias → 乘 a_log_exp → gate_scale × sigmoid）与 torch_ref 的 [tests/torch_ref.py:161-169](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L161-L169) 逐步对齐。

两个数值细节：

- **`a_log_exp = expf(A_log_ptr[head_idx])`** 在 [csrc/smxx/fwd_kernel1.cuh:257-258](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L257-L258)，看似普通 `expf`，实则在 `--use_fast_math` 下编译为 `ex2.approx.ftz(x * log2e)`——所以 torch_ref 写 `fp32_ex2_ftz(A_log * LOG2E)` 才能逐位对上。
- **sigmoid 用 tanh 近似**：`sigmoid_tanh_approx_f32`（[csrc/smxx/utils.cuh:50-53](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L50-L53)）实现 \( \sigma(x) = \tanh(x/2)/2 + 1/2 \)，依赖 `tanh.approx.f32` 指令；torch_ref 用一个内联同样 PTX 的小扩展 `sigmoid_ext`（[tests/torch_ref.py:7-29](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L7-L29)）复刻。**近似 sigmoid 的结果在这里保持 fp32，不量化 bf16**。

cumsum 的对照：kernel 是每线程 16 次顺序 `sum += g_val`；torch_ref 是先零填充 `g_chunk` 再 `g_chunk.cumsum(dim=0)`、`g_total = g_cumsum[-1:]`（[tests/torch_ref.py:208-209](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L208-L209)）。零填充 +1 等价于 kernel 的 `gv=0` 分支；exact-match 测试通过的事实说明这两条路径在 16 行长度上产生完全相同的比特。

另外注意 smem 生命周期：`g_bf16`（TMA 落地缓冲）与 `dt_bias` 都在本段之后死亡——它们的 union 伙伴 `k_restored`、`g_total` 随后接管（见 u2-l8 详述）。

#### 4.2.4 代码实践

**实践目标**：用纯 Python 验证「padding 行贡献 0 ⇒ cumsum 冻结、g_total 只含真实行」。

**操作步骤**：保存为 `gate_cumsum_check.py` 并运行（无需 GPU；sigmoid 用精确版本，仅验证结构不验证近似）。

```python
# 示例代码：单列的门控激活 + inclusive cumsum（模拟线程 col 的视角）
import random, math
def gate_cumsum_col(g_raw_col, dt, a_log_exp, gate_scale, actual_len):
    out, s = [], 0.0
    for row in range(16):
        gv = 0.0
        if row < actual_len:
            x = a_log_exp * (g_raw_col[row] + dt)
            gv = gate_scale * (1.0 / (1.0 + math.exp(-x)))  # 精确 sigmoid 代替 tanh 近似
        s += gv
        out.append(s)
    return out, s

random.seed(0)
g_raw  = [random.uniform(-1, 1) for _ in range(16)]
actual_len = 11                                   # 尾块：只有前 11 行是真 token
cum, g_total = gate_cumsum_col(g_raw, 0.3, 1.7, -5.0 * math.log2(math.e), actual_len)

assert all(cum[r] == cum[actual_len - 1] for r in range(actual_len, 16))  # 冻结
valid_sum = sum(cum[r] - (cum[r-1] if r else 0.0) for r in range(actual_len))
assert abs(g_total - valid_sum) < 1e-9                                     # 只含真实行
print(f"OK: g_total={g_total:.4f}, padding 行 cumsum 冻结于 {cum[15]:.4f}")
```

**需要观察的现象**：row ≥ actual_len 的 cumsum 值全部等于 `cum[actual_len-1]`；`g_total` 等于前 `actual_len` 行激活值之和。

**预期结果**：打印 `OK: ...`。想一想：若把 `gv = 0.0` 分支删掉（padding 行照常激活），`g_total` 会多算 5 行的遗忘量，K2 的块级状态衰减 `2^g_total` 会系统性地多衰减——这就是尾块 final_state 出错的根源。

#### 4.2.5 小练习与答案

**练习 1**：为什么 cumsum 不做并行扫描（如 Blelloch scan）？
**答案**：只有 16 个元素、且一个线程顺带完成激活，寄存器内 16 次加法比任何并行方案（需要 smem 往返 + 同步）都便宜。并行 scan 的收益要从长度远大于线程数的序列才能体现。

**练习 2**：为什么门控激活三步（+dt_bias → ×a_log_exp → gate_scale×sigmoid）的**顺序**不能随手调换？
**答案**：浮点乘加不满足结合/交换的逐位等价；torch_ref 按同一顺序书写，任意调换都可能产生最后一位的差异，`torch.equal` 断言就会失败。bit-exact 测试约束了实现顺序 = 规格顺序。

**练习 3**：清零 k 尾行为什么放在门控分支里「顺便」做，而不是单独一步？
**答案**：两个任务互不依赖（不同线程、不同缓冲），拼在同一分支内可以让 256 线程同时有活干，把两步的同步合并成一次 `__syncthreads()`（L339）。

---

### 4.3 decay_apply：寄存器分块一次性生成四个衰减变体

#### 4.3.1 概念说明

decay_apply 是 K1 计算密度最高的一段：把已归一化的 q/k 与 log2 域 cumsum 组合成 u2-l1 定义的四件套（全部在 log2 域，`ex2` 即自然域的 \( e^{(\cdot)} \)）：

\[
\begin{aligned}
k\_decayed_{t,d} &= k_{t,d} \cdot bf16(2^{gc_{t,d}}) \\
q\_decayed_{t,d} &= q_{t,d} \cdot bf16(2^{gc_{t,d}}) \cdot bf16(scale) \\
k\_inv_{t,d} &= k_{t,d} \cdot bf16(2^{-gc_{t,d}}) \\
k\_restored_{t,d} &= k\_inv_{t,d} \cdot bf16(2^{g\_total_d})
\end{aligned}
\]

两个结构性问题决定了代码形态：

1. **别名问题**：输出缓冲 k_decayed/q_decayed/k_inv 与输入 q/k 在同一个 smem union 的两侧；k_restored 又别名 g_bf16。所以必须**两段式**：先把 q/k/g/g_total 全部搬进寄存器，`__syncthreads()`，再写输出——绝不能边读边写。
2. **分块问题**：16×128 = 2048 个元素、256 线程，每线程 8 个元素。怎么切？kernel 选了 8×64 的 tile（`N_M = CHUNK/8 = 2`，`N_N = D/64 = 2`，共 4 个 tile），每个 tile 内「8 warp × 32 lane × 2 元素 = 512」精确覆盖。

#### 4.3.2 核心流程

线程-数据映射是本模块的核心。每个线程先算出自己的身份：

```text
lane = tid % 32;  warp_id = tid / 32
g = lane / 4   ∈ [0,8)   # 同时决定「行偏移」与「列段」
t = lane % 4   ∈ [0,4)   # 段内 2-元素对的选择

对每个 tile (m_blk ∈ {0,8}, n_blk ∈ {0,64}):        # tile_idx = (m_blk/8)*2 + n_blk/64
    row      = m_blk + ((warp_id + g) % 8)           # 本线程负责的行
    col_base = n_blk + g*8                           # 本线程负责的 8 元素列段
    元素      = (row, col_base + 2t) 与 (row, col_base + 2t + 1)
```

**覆盖性论证**（实践任务会机器验证）：固定一个 tile（8 行 × 64 列 = 512 元素）：

- **行**：对任意 warp_id，g 取 0..7 时 `(warp_id+g)%8` 恰好枚举 0..7——8 行全覆盖；
- **列**：g 同时选定了列段 `g*8 ∈ {0,8,…,56}`——64 列切成 8 段全覆盖；段内 t 取 4 个 2-元素对；
- **无碰撞**：若两个线程同行，则 `(w1+g1) ≡ (w2+g2) (mod 8)`；若 w1≠w2 必然 g1≠g2，列段不同；若 w1=w2 则 g1=g2（同一行同一段），再看 t 即可区分。所以每个 (row, col) 恰有一个线程负责。

每线程的寄存器足迹：`reg_g/reg_gt[4][2]`（fp32）+ `reg_q/reg_k[4][2]`（bf16）——4 个 tile × 2 元素。**为什么每线程抓 2 个连续 bf16**：恰好一个 32 位字，读写都能向量化（`AutoVectorizingCopy`），且这个「连续 4 lane 覆盖一个 8 元素行段」的排布正是 LDSM/GMMA fragment 的期望形状——后续 L/Mqk 的 MMA 才能零 shuffle 直读。

随后两段式执行：

```text
循环一（读）：按上映射把 g_cumsum / q / k / exp(g_total) 装进 reg_*       L384-L421
__syncthreads()        # union 别名：读写两侧缓冲切换的唯一安全点           L425
循环二（写）：对每个 tile、每个 v∈{0,1}：
    exp_cumsum = BF16(ex2(reg_g))            # fp32 指令，入口处才量化 bf16
    q_decayed  = q * exp_cumsum * BF16(scale)   # 两次 bf16 乘法 = 两次舍入
    k_decayed  = k * exp_cumsum
    inv_cumsum = BF16(ex2(-reg_g))
    k_inv      = k * inv_cumsum
    k_restored = k * inv_cumsum * BF16(reg_gt)  # reg_gt 已是 exp(g_total)
```

#### 4.3.3 源码精读

- **映射与寄存器装载**：[csrc/smxx/fwd_kernel1.cuh:360-421](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L360-L421)。`local_tile(g_tile, vec8_2d, make_coord(row, col_tile))` 在 (16,128) 张量上切出 (1,8) 的行段，再 `local_tile(..., thr2_2d, make_coord(0, t))` 取出 2 元素对；`AutoVectorizingCopy` 把它们装进寄存器数组。开头的两个 `static_assert(D % 64 == 0)`、`static_assert(CHUNK % 8 == 0)` 固化了 8×64 分块的约束。
- **别名屏障**：[csrc/smxx/fwd_kernel1.cuh:423-425](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L423-L425) 的注释直说动机：写 union 对侧缓冲前必须全员读完。`compute_tid < 256` 恒真（K1 固定 256 线程，见 [csrc/smxx/fwd_launch.cu:149](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L149) 的 `kK1Threads = 256`），所以这里的 `__syncthreads()` 全员可达、合法。
- **计算与写回**：[csrc/smxx/fwd_kernel1.cuh:427-473](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L427-L473)。注意 [L449-L456](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L449-L456) 里 `float g = reg_g[tile_idx][v];` **遮蔽（shadow）**了外层的线程索引 `int g = lane / 4`——两处 `g` 含义完全不同（一个是 cumsum 数值、一个是 lane 分组号），阅读时极易踩坑；L464 同理。
- **g_total 的预变换**：[csrc/smxx/fwd_kernel1.cuh:353-358](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L353-L358) 在 decay_apply 之前，由线程 0-127 把 smem 里的 `g_total(col)` 就地替换成 `ex2(g_total(col))`。因此 `reg_gt` 装的已经是 \( 2^{g\_total} \)，写 k_restored 时直接 `BF16(reg_gt)`。**连带效应**：最终 TMA store 到 workspace 的 g_total 段存的是 \( e^{g\_total} \)（乘性整块衰减因子）而不是 \( g\_total \) 本身——K2 拿到即可直乘（承接 u2-l2 的 workspace 契约）。

与 torch_ref 的逐条对账（[tests/torch_ref.py:208-215](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L208-L215)）：

| kernel 表达式 | torch_ref 对应行 | 舍入链 |
|---|---|---|
| `BF16(ex2_approx_ftz_f32(g))` | `fp32_ex2_ftz(g_cumsum).to(bf16)`（L210） | fp32 指令 → 1 次 bf16 量化 |
| `q * exp_cumsum * BF16(scale)` | `q_chunk * exp(...).to(bf16) * scale_bf16`（L211） | 2 次 bf16 乘法 = 2 次舍入 |
| `k * exp_cumsum` | `k_chunk * fp32_ex2_ftz(...).to(bf16)`（L210） | 1 次舍入 |
| `BF16(ex2(-g))`；`k * inv_cumsum` | `fp32_ex2_ftz(-g_cumsum).to(bf16)`；L213 | 1 次舍入 |
| `k * inv_cumsum * BF16(reg_gt)` | `k_inv * g_total_exp_bf16`（L214-215） | 共 2 次舍入（先 k_inv 后乘 e^{g_total}） |

特别地，k_restored 的两次 bf16 舍入顺序（先 `(k·2^{-gc})` 舍入、再乘 \( 2^{g\_total} \) 舍入）在两侧严格一致——这正是「参考实现模拟硬件行为」的含义：连中间量的量化点都要复刻。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：把 decay_apply 的线程-数据映射翻译成 Python，机器验证「16×128 矩阵被 256 线程完整覆盖且无重叠」，并打印线程 0 与线程 33 的映射作为样例。

**操作步骤**：保存为 `decay_map.py`（纯 Python，无需 GPU），运行 `python decay_map.py`。

```python
# decay_map.py —— decay_apply 线程映射的纯 Python 复刻（对照 fwd_kernel1.cuh L360-L475）
CHUNK, D = 16, 128

def thread_map(tid):
    """输入 warp_id/lane（由 tid 派生）与推导出的 g/t，
    返回该线程在 4 个 tile 中负责的 (tile_idx, row, col0, col1) 及读写的寄存器。"""
    lane, warp_id = tid % 32, tid // 32
    g, t = lane // 4, lane % 4            # g: 行偏移+列段; t: 段内 2 元素对
    plan = []
    for m_blk in range(0, CHUNK, 8):      # {0, 8}
        for n_blk in range(0, D, 64):     # {0, 64}
            tile_idx = (m_blk // 8) * (D // 64) + n_blk // 64
            row   = m_blk + ((warp_id + g) % 8)
            c0, c1 = n_blk + g * 8 + 2 * t, n_blk + g * 8 + 2 * t + 1
            regs = {  # 该 (tile_idx, v) 对应的寄存器槽位
                "reg_g[t%d][0..1]" % tile_idx:  [c0, c1],   # g_cumsum(row, c*)（fp32）
                "reg_q[t%d][0..1]" % tile_idx:  [c0, c1],   # q(row, c*)（bf16）
                "reg_k[t%d][0..1]" % tile_idx:  [c0, c1],   # k(row, c*)（bf16）
                "reg_gt[t%d][0..1]" % tile_idx: [c0, c1],   # exp(g_total(c*))（fp32）
            }
            plan.append((tile_idx, row, c0, c1, regs))
    return plan

def check_coverage():
    from collections import Counter
    hits = Counter()
    for tid in range(256):
        for _, row, c0, c1, _ in thread_map(tid):
            hits[(row, c0)] += 1
            hits[(row, c1)] += 1
    assert sum(hits.values()) == CHUNK * D, "总数 != 2048"
    assert set(hits.values()) == {1}, "存在重叠或遗漏"
    # 每个 tile 内部也应是 8x64=512 个互异元素（行/列段随 tile 平移）
    print("OK: 16x128 = 2048 个元素被 256 线程各负责一次，无重叠、无遗漏")

for tid in (0, 33):
    print(f"-- tid={tid} (warp_id={tid//32}, lane={tid%32}) --")
    for tile_idx, row, c0, c1, _ in thread_map(tid):
        print(f"  tile{tile_idx}: row={row:2d} cols=({c0:3d},{c1:3d})")

check_coverage()
```

**需要观察的现象**：tid=0（warp0/lane0，g=0,t=0）负责 4 个 tile 的 `(row, col)` = (0,0-1)、(0,64-65)、(8,0-1)、(8,64-65)；tid=33（warp1/lane1，g=0,t=1）行相同但列对右移 2（(0,2-3)、(0,66-67)、(8,2-3)、(8,66-67)）——体现「warp_id 决定行、g 决定行偏与列段、t 决定段内对」。

**预期结果**：打印两张映射表 + `OK: 16x128 = 2048 ...`。可以再改动试试：把 `row = m_blk + ((warp_id + g) % 8)` 改成 `row = m_blk + g`（去掉 warp_id），断言会立刻失败——因为这样 8 个 warp 会重复覆盖同样的 8 行（只有 64×8=512… 恰好一半元素没人管、一半被 8 个线程争抢），直观展示 `% 8` 中 warp_id 的作用。

#### 4.3.5 小练习与答案

**练习 1**：循环一与循环二之间的 `__syncthreads()`（L425）删掉会发生什么？
**答案**：未定义行为。k_decayed 等输出缓冲与 q/k 输入缓冲 union 别名：跑得快的线程开始写 k_decayed 时，跑得慢的线程可能还没把对应的 q/k 读进寄存器，读到被覆盖的脏数据。注释「Safe: all 256 threads enter this if block」还提醒了另一面：`__syncthreads()` 必须全员到达，`compute_tid < 256` 恒真保证了这一点。

**练习 2**：不运行代码，口算一个 tile（8×64）如何被 8 个 warp 分完。
**答案**：每个 warp 内 8 个 g 值给出 8 个互不相同的行（`(w+g)%8` 遍历 0..7）与 8 个互不相同的列段（`g*8`），32 lane × 2 元素 = 64 元素 = 8 行×8 列段中每行每段取一对……合计 8 warp × 64 = 512 = 8×64，恰好铺满且由练习中的无碰撞论证保证不重叠。

**练习 3**：`reg_gt` 里为什么可以直接存 `exp(g_total)` 而不用担心像 cumsum 那样的行间差异？
**答案**：g_total 是**每列一个**的标量（对整个 chunk 而言是「整块总遗忘」），没有行维度；decay_apply 里它只参与 k_restored 的逐元素乘法，与行无关。真正随行变化的是 `reg_g` 里的 cumsum。

---

### 4.4 L 与 Mqk 的构造：单 warp MMA 与 tril+beta 融合掩码

#### 4.4.1 概念说明

四个衰减变体就位后，K1 要造出块内三件套的前两件（u2-l1）：

\[
L = \mathrm{tril}\big(\,k\_decayed \cdot k\_inv^{\top}\,,\,-1\big) \cdot \mathrm{diag}(\beta), \qquad
Mqk = \mathrm{tril}\big(\,q\_decayed \cdot k\_inv^{\top}\,\big)
\]

两者都源自 16×128×128 的 GEMM，但**下游用途不同导致精度不同**：

- **L 是求逆的种子**（下一步 `inv_fwd_subst_fused_1warp` 的输入），全程 fp32，beta 以 fp32 乘入，不做任何 bf16 量化；
- **Mqk 直接进入 K2 的输出计算** \( out \mathrel{+}= Mqk \cdot U \)，以 bf16 存储（fp32 累加、出口一次量化）。

**对角线的差异**是「先擦后写、写完再读」语义的直接体现：第 \( i \) 个 token 的输出读到的是**它自己写入之后**的状态，所以 Mqk 保留 \( j = i \) 项（`tril` 默认含对角）；而 \( (I+L) \) 的单位对角已经表达了「自身对自身」，L 只需严格下三角（`diagonal=-1`）。

#### 4.4.2 核心流程

```text
warp 0（tid 0..31）:  C = k_decayed @ k_invᵀ   → fp32 累加，原样存 L_fp32     （两 warp 并行、无依赖）
warp 1（tid 32..63）: C = q_decayed @ k_invᵀ   → fp32 累加，出口量化 bf16 存 Mqk
其余 192 线程：直接到 __syncthreads() 等待

__syncthreads()

掩码步（256 线程，每线程恰 1 个 (i,j)）：
    col_block_size = 8
    block_idx = tid / 128            # 左半 8 列 or 右半 8 列
    i = (tid / 8) % 16               # 行
    j = tid % 8 + block_idx * 8      # 列
    if i <= j: L(i,j) = 0                          # 严格下三角
    else:      L(i,j) = L(i,j) * sigmoid(beta[i])   # fp32 乘入激活后的 beta
    if i <  j: Mqk(i,j) = 0                         # 含对角
```

掩码步的精妙在于「**同线程同元素**」：每个线程对 `L_fp32(i,j)` 做读-改-写，而该元素没有任何其他线程会碰，Mqk 的清零又在另一个缓冲——因此**步内不需要任何同步**，只需在 MMA 完成（L487 的 `__syncthreads()`）之后开始、在求逆之前结束（L508 再同步）。

#### 4.4.3 源码精读

- **两个单 warp MMA**：[csrc/smxx/fwd_kernel1.cuh:478-487](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L478-L487)。warp 0 调 `mma_m16n16_bf16bf16fp32_1warp(k_decayed, k_inv, L_fp32, ...)`，warp 1 调 `mma_m16n16_bf16bf16bf16_1warp(q_decayed, k_inv, Mqk, ...)`。两个帮手函数在 [csrc/smxx/utils.cuh:145-165](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L145-L165)（bf16 存储，带 `sC_store_op` 把 fp32 累加器逐元素转 bf16）与 [csrc/smxx/utils.cuh:167-192](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L167-L192)（fp32 累加器原样写回）。二者都用 `SM80_16x8x16_F32BF16BF16F32_TN` atom 配 `Tile<_16,_16,_16>` 拼成一个 16×16 的 GEMM，K 维 128 在 `cooperative_gemm` 内部循环；操作数经 `SM75_U32x4_LDSM_N` 直接从 swizzled smem 以 fragment 形式读入——decay_apply 铺好的 MMALayout 在这里兑现价值。`if (mma_tid >= int(size(mma))) return;` 保证只有一个 warp 的 32 线程真正参与。
- **tril + beta 融合掩码**：[csrc/smxx/fwd_kernel1.cuh:493-507](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L493-L507)。256 线程 × 1 元素 = 256 = 16×16 恰好铺满；`BF16::bitcast(0)` 写死全零位。beta 的激活用与 4.2 相同的 `sigmoid_tanh_approx_f32`，但**保持 fp32**（L 是 fp32）；`beta_tile(beta_smem_offset + i)` 的 `beta_smem_offset = (head_idx*T_total + bos + local_t*CHUNK) & 7` 正是 u2-l6 讲过的 1D TMA `& ~7` 向下对齐的消费端余量（[csrc/smxx/fwd_kernel1.cuh:345](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L345)）。
- **torch_ref 对照**：[tests/torch_ref.py:216-223](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L216-L223)。`torch.mm(k_decayed, k_inv.t(), out_dtype=torch.float32)` 对应 L 的 fp32 存储；`torch.matmul(q_decayed, k_inv.t())`（bf16 出口）对应 Mqk；`torch.tril(L, diagonal=-1) * beta_activated.unsqueeze(-1)` 与 `torch.tril(Mqk)` 分别对应 `i <= j` 与 `i < j` 两个分支，`beta_activated` 同样是 fp32 的 tanh 近似 sigmoid（[tests/torch_ref.py:220-221](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L220-L221)）。

一个自洽性细节：beta 的尾行（row ≥ actual_len）读的是 smem 里的垃圾，但 L 的尾行此时已经全为 0（k_decayed 尾行为 0 ⇒ GEMM 结果为 0），`0 × 垃圾 = 0`，垃圾被精确消灭——这是 4.2 清零 k 尾行带来的连锁保障。

之后 kernel 调 [csrc/smxx/utils.cuh:213-277 附近](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L213-L277) 的 `inv_fwd_subst_fused_1warp(L_fp32, M_bf16, INV, ...)`（[csrc/smxx/fwd_kernel1.cuh:510-511](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L510-L511)）完成求逆——注意合并矩阵 M 被暂存进**已死亡的 k_inv 缓冲**（L490-491 的注释），这是 smem 复用的又一例。求逆算法本身留给 u3-l1。

#### 4.4.4 代码实践

**实践目标**：验证掩码步的线程映射 `i = (tid/8)%16, j = tid%8 + (tid/128)*8` 与 `torch.tril` 语义逐元素等价。

**操作步骤**：保存为 `mask_check.py`，CPU 上运行 `python mask_check.py`（需要安装 torch）。

```python
# 示例代码：掩码线程映射 vs torch.tril
import torch
torch.manual_seed(0)
L, M = torch.randn(16, 16), torch.randn(16, 16)
Lk, Mk = L.clone(), M.clone()

for tid in range(256):                       # 对照 fwd_kernel1.cuh L494-L507
    block_idx = tid // 128                   # col_block_size = 8
    i = (tid // 8) % 16
    j = tid % 8 + block_idx * 8
    if i <= j: Lk[i, j] = 0.0                # 严格下三角（对角也清零）
    if i <  j: Mk[i, j] = 0.0                # 含对角

assert torch.equal(Lk, torch.tril(L, diagonal=-1))
assert torch.equal(Mk, torch.tril(M))
print("OK: 掩码映射 == tril(-1) / tril()，且 (i,j) 恰被一个线程覆盖")
```

**需要观察的现象**：两条断言通过；`(i, j)` 在 tid ∈ [0,256) 上是双射（左半 128 线程管 j∈[0,8)，右半管 j∈[8,16)）。

**预期结果**：打印 `OK: ...`。再想一想：掩码步若放在两个 MMA **之前**，结果会如何？（beta 会乘在未定义的 smem 垃圾上，且 tril 掩不掉 MMA 重新写入的完整矩阵——顺序不可换。）

#### 4.4.5 小练习与答案

**练习 1**：为什么 warp 0 和 warp 1 的两个 MMA 之间不需要 `__syncthreads()`？
**答案**：它们写不同缓冲（L_fp32 vs Mqk），读的 k_decayed/q_decayed/k_inv 都是只读；唯一的同步点是两个 MMA 都完成之后（L487），让掩码步看到完整的 16×16 结果。

**练习 2**：Mqk 的 bf16 出口量化发生在哪一行语义上？torch_ref 用哪行对拍？
**答案**：kernel 侧在 `mma_m16n16_bf16bf16bf16_1warp` 的 `sC_store_op`（utils.cuh L162：`BF16(x)`，fp32 累加器逐元素转 bf16）；torch_ref 侧是 [tests/torch_ref.py:217](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L217) 的 `torch.matmul(q_decayed, k_inv.t())`（bf16 输入、bf16 输出，内部 fp32 累加）。

**练习 3**：把掩码步的 `L_fp32(i,j) = L_fp32(i,j) * sigmoid(...)` 拆成「先全乘 beta 再单独 tril」两步，数值上会变吗？
**答案**：不会——乘法与清零作用在不同元素集合上（`i>j` vs `i<=j`），交换不改变任何最终元素的值或舍入路径。真正不可交换的是「掩码必须在 MMA 之后、求逆之前」这一顺序约束。

---

## 5. 综合实践

**任务：单 tile 四阶段 CPU 复刻 `kernel1_stages.py`**——把本讲四个阶段串成一个脚本，对同一个 16×128 tile 按 kernel 的精确顺序重演，并验证四条结构性不变量。全程 CPU 可跑（用 `.to(torch.bfloat16)` 模拟量化点，用 fp64 做纯数学对照）。

**操作步骤**：

1. 随机生成一个 tile 的原始输入：`q_raw/k_raw` bf16 [16,128]、`g_raw` bf16 [16,128]、`beta_raw` bf16 [16]、`A_log` 标量、`dt_bias` fp32 [128]，取 `scale=128**-0.5`、`lower_bound=-5.0`、`actual_len=11`（尾块）。
2. 按顺序复刻：
   - ① L2 归一化（朴素版即可，`x / (x.norm(dim=-1, keepdim=True) + 1e-6)`，bf16 写回）；
   - ② 门控激活 + cumsum（精确 sigmoid + `lower_bound*log2(e)` 换底），k 的 row ≥ 11 清零，产出 `g_cumsum/g_total`；
   - ③ `exp_g_total = exp2(g_total)`；
   - ④ 四个衰减变体（**按 kernel 的舍入链**：`bf = lambda x: x.to(torch.bfloat16).float()`，`exp_cumsum = bf(torch.exp2(g_cumsum.float()))`，`q_decayed = bf(bf(q.float()*exp_cumsum) * bf(scale))` 等）；
   - ⑤ `L = tril(k_decayed @ k_inv.T, -1) * sigmoid(beta)`（fp32）、`Mqk = tril(q_decayed @ k_inv.T)`（bf16）。
3. 验证不变量并打印结果：
   - (a) `g_total` 只含前 11 行的贡献（`g_cumsum[15] == g_cumsum[10]`）；
   - (b) `k_decayed/k_inv/k_restored` 的 row ≥ 11 全为 0；
   - (c) `k_restored ≈ k * exp2(g_total - g_cumsum)`（bf16 相对容差 ~1e-2 内，两条舍入链不同属正常）；
   - (d) `L` 严格下三角、`Mqk` 含对角。
4. （可选，需 GPU）安装好 FlashKDA 后，把同一组输入喂给 `tests/torch_ref.py` 的 `torch_ref`，`torch.equal` 对拍你的 `L`（fp32 逐位）与 `Mqk/k_decayed`（bf16 逐位）——若逐位一致，说明你复刻的舍入链正确；若不一致，排查 ④ 中哪一步少了一次 `bf()`。**待本地验证**（本讲义编写环境无 GPU，未实际运行）。

**需要观察的现象 / 预期结果**：四条不变量全部通过；(c) 中两条舍入链的相对误差在 bf16 精度（约 \(10^{-2} \) 量级）内但不逐位相等——这正是 4.3.3 舍入链表想要让你亲眼看到的事实：**同一个数学公式、不同的量化点排布，结果不同**。

## 6. 本讲小结

- K1 计算主体是四级流水：L2 归一化 → 门控激活+cumsum ∥ k 尾清零 → decay_apply（两段式：先读进寄存器、sync、再写 union 对侧）→ 双单 warp MMA 构造 L/Mqk + tril+beta 融合掩码。
- L2 归一化用「每线程 8 元素顺序 FMA + 16-lane xor 蝶形归约 + rsqrt」，torch_ref 用 `fp32_fma` 与相同的 offset 序列逐位复刻。
- 门控分支一石二鸟：128 线程做激活+cumsum（padding 行贡献 0 ⇒ g_total 只含真实 token），另 128 线程清零 k 尾行——后者是状态更新 \( \delta s = k\_restored^\top U \) 不被尾块垃圾污染的正确性根源；q 尾行不清零是精确最小化。
- decay_apply 的映射 `row = m_blk + ((warp_id+g)%8)、col = n_blk + 8g + 2t + v` 让每线程抓 2 个连续 bf16（一个 32 位字），既向量化又与 LDSM fragment 形状对齐；8×64 tile × 4 恰被 256 线程无重叠铺满（已用 decay_map.py 机器验证）。
- 所有指数都在 log2 域（host 预乘 `lower_bound·log2e`），`ex2.approx.ftz` 只在 fp32 中求值、入口处才量化 bf16；L 全程 fp32、beta 以 fp32 乘入，Mqk 则在 MMA 出口量化 bf16——「哪个量在哪个精度停留」由下游用途决定。
- 掩码步「同线程同元素」的读-改-写让 tril 与 beta 融合无需步内同步；`i<=j` / `i<j` 两个条件分别对应 `tril(-1)`（擦除发生在写入前）与 `tril()`（读出发生在写入后）。

## 7. 下一步学习建议

- **u2-l8（workspace 契约）**：本讲四个衰减变体与 L/Mqk/INV/g_total 的产物如何经六次 TMA store 写入 workspace、K2 又如何按 `ws_idx = head*total_tiles + tile` 对称读取，以及 K1 smem union 的完整生命周期图。
- **u3-l1（16×16 求逆）**：本讲留下的悬念 `inv_fwd_subst_fused_1warp`——分块前代换 + bf16 HMMA 合并，以及它为何取代 fp16 Neumann 级数。
- **动手线索**：把本讲的 `decay_map.py` 扩展成「K1 全 256 线程的活动时间线」——标出每个线程在四个阶段各自负责的元素数（8:8×2:8:1），直观感受负载均衡；或用 `--ptxas-options=-v` 重编译，观察 decay_apply 的寄存器数组对 K1 寄存器占用的影响（承接 u1-l3 的构建知识）。
