# 领域 epilogue：rotary、scaled_exp、量化输出

## 1. 本讲目标

前几讲（u6-l1 ~ u6-l4）建立了可组合 epilogue 的骨架：`EpiOp` 生命周期、`fn_port` 端口协议、`@gemm_epilogue` 函数式创作，以及默认线性 epilogue `D = α·D + β·C + r_n + c_m`。本讲离开「骨架」，进入 `quack/epilogue/` 下四个**领域模块**（domain modules）——它们是把骨架拼成真实 transformer 算子的肉。

学完本讲你应能：

1. 说清 **rotary（RoPE）** epilogue 如何把「加载/计算 cos·sin」抽象成一个 `value` 端口 op，并理解两种实现（表加载 vs 内核内 sincos）的取舍。
2. 掌握 **`GroupedColStatsBase` 预扫描统计**机制：在主存储前先对原始累加器算一个分组统计量（如每头 rstd），再以「逐行值」喂回主函数。`HeadRstd` 是它最干净的实例。
3. 理解 **scaled_exp / LSE 归约**：两阶段稳定 exp 存储（幂次偏移 + LSE partials），以及 `ColVecReduce` / `OnlineLSEReduce` / `ColVecSelect` 三种「沿 N 归约/选取」输出。
4. 读懂 **`quantize_out` 量化输出**：`BlockScaleFactorStore` 如何在 store 前对最终 fragment 做原地量化、写出 blocked scale factors（SFD），以及它与 `DStore` 的协作。

## 2. 前置知识

本讲假设你已熟悉以下来自 u6-l1 ~ u6-l4 的概念，这里只做最小复述：

- **`@gemm_epilogue` 函数契约**：`fn(acc, **operands) -> {"D": ..., <outputs/sinks>...}`，对每个累加器元素（或 pair）调用一次。计算顺序由源码显式写明。
- **`fn_port` 端口**：`row`/`col`/`tile`/`scalar`（内置片段）、`value`（自定义逐元素值源 op）、`apply`（可调用）、`sink`（函数返回值，按片段收集后 `fn_sink_flush`）。前端只按 `fn_port` 分发，绝不 `isinstance`。
- **`mode="acc_pair"` 与 `unpack`/`pack`**：累加器在相邻 N 列上成对，`x1, x2 = unpack(acc)`，`pack(a, b)` 把两 lane 写回。RoPE 天然成对。
- **EpiOp 生命周期**：主机侧 schema 三件套 `host_arg_key`/`host_fake_arg`/`host_call_arg` + 设备侧 `begin`/`begin_loop`/`end_loop`/`end`。
- **`convert_layout_zero_stride`**：把广播（零步长）布局重打包成 `(真实, 广播)` 二模，让行/列向量归约在寄存器内正确累加（u3-l2）。

一个贯穿全讲的关键直觉：**这些领域 op 不是新框架，而是新 `EpiOp` + 一个端口方法**。写一次资源生命周期，加一个端口，就能和其它所有 op 组合，主机管线、缓存、启动全部继承自 schema。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [quack/epilogue/rotary.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/rotary.py) | RoPE 资源与即用 epilogue：cos/sin 表加载 op（TMA / LDG 两条路）、内核内 sincos 的 float-float turns 数学、以及 `rope_table_epi`/`rope_posfreq_epi`/`mrope`/`xpos` 等变体和主机端表构造器。 |
| [quack/epilogue/head_rmsnorm.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/head_rmsnorm.py) | `HeadRstd`：每头 rstd 统计 op，是 `GroupedColStatsBase` 最小的真实实例。 |
| [quack/epilogue/scaled_exp.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/scaled_exp.py) | 两阶段稳定 exp 存储 + LSE partials：`MaxLog2` 预扫描 max、`scaled_exp_epi` 幂次偏移存储 + `sum_exp` 归约、CE-eval 的 target-logit 变体。 |
| [quack/epilogue/ops.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py) | EpiOp 词表（u6-l2）。本讲重用 `ColVecReduce`、`OnlineLSEReduce`、`ColVecSelect`，并精读 `GroupedColStatsBase`/`GroupedColStatsOut`。 |
| [quack/epilogue/quantize_out.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/quantize_out.py) | `BlockScaleFactorStore`：量化输出（SFD）op——amax、量化 SF、原地 rescale、blocked 布局写出，row/col 两方向。 |
| [quack/epilogue/library.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/library.py) | 即用 mod 库。本讲引用 `head_rmsnorm_epi`/`qknorm_epi`/`lse_epi`/`lse_target_epi`/`gated_quant_mod` 等组装实例。 |
| [quack/blockscaled/quantize_utils.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/blockscaled/quantize_utils.py) | 量化核心：`quantize_sf_slots`、`sf_vec_size_for`、`QUANT_DTYPE_MAX`，被 `BlockScaleFactorStore` 共享。 |

## 4. 核心概念与源码讲解

### 4.1 Rotary 旋转位置（RoPE）

#### 4.1.1 概念说明

RoPE（旋转位置编码）对每个「旋转对」\((x_1, x_2)\)，按行位置 `pos` 与该对的频率 `inv_freq` 决定的角度 \(\theta\) 做一次二维旋转：

\[
(x_1, x_2) \mapsto (x_1\cos\theta - x_2\sin\theta,\; x_1\sin\theta + x_2\cos\theta)
\]

在 GEMM epilogue 里，累加器天然在**相邻 N 列**上成对（这正是 `mode="acc_pair"`），所以 `unpack(acc)` 直接拿到 \((x_1, x_2)\)。难点不在数学，而在「cos/sin 从哪来」——这正是 RoPE 资源 op 要封装的。QuACK 给两条路：

- **表加载（table）**：主机预算好 `(seqlen_ro, head_dim)` 的交错 cos/sin 表（偶数列为 cos、奇数列为 sin），内核里加载。这是「值源 op」。
- **内核内 sincos（posfreq）**：只传「逐行位置」colvec 和「逐列交错 inv-freq」rowvec，内核里用 `sincos(pos * inv_freq)` 现算。

两条路都暴露成同一个**函数式组合点**：函数体里写 `c, s = unpack(cs)`（或 `s, c = _sincos_turns(...)`），旋转顺序显式、可审阅。

#### 4.1.2 核心流程

表加载路（默认走 TMA）：

1. 主机用 `make_interleaved_cos_sin` 把 HF 风格的 `(seqlen, head_dim/2)` cos/sin 交错成 `(seqlen, head_dim)`。
2. 内核 `begin` 构造「每个 subtile 的 gmem cos/sin 片段」，要求 `tile_N % head_dim == 0`（一个 tile 覆盖整数个头，用 stride-0 模重复）或 `head_dim % tile_N == 0`（一个头跨多个 tile，切片）。
3. `begin_loop` 返回与累加器逐元素对齐的 cos/sin 片段（`value` 端口），双缓冲预取下一 subtile。
4. 函数体 `rope_table_epi`：`unpack(acc+bias)` → 旋转 → `pack` 写回 D。

posfreq 路：位置和频率都是普通广播向量加载（`tile_M + tile_N` 个元素流量，远小于表的 `tile_M * head_dim`），内核内做 float-float turns 数学算 sincos。

#### 4.1.3 源码精读

函数体只有三行，旋转顺序一目了然（`mode="acc_pair"` 表示 acc 成对）：

[quack/epilogue/rotary.py:323-329](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/rotary.py#L323-L329) —— `rope_table_epi`：`cs` 是 cos/sin 表 op 的值端口，`bias` 是 rowvec，旋转数学显式写成 `x1*c - x2*s, x1*s + x2*c`。

表 op 的工厂在 TMA 与 LDG 间二选一：

[quack/epilogue/rotary.py:294-320](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/rotary.py#L294-L320) —— `rotary_cos_sin_load(name, tma=True)`。注释里有一张实测表：在 pingpong 大 tile 下，LDG 双缓冲的寄存器代价会把组合 epilogue 推向溢出（spills），而 TMA 把预取交给 producer warp、逃出 per-warpgroup 的独占 epilogue 窗口，故默认 TMA。

表加载的核心巧思在 `begin` 里用**静态布局代数**表达「head 广播」：

[quack/epilogue/rotary.py:62-79](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/rotary.py#L62-L79) —— 当 `tile_N % head_dim == 0`，一个 tile 覆盖 ≥1 个完整头，用 stride-0 模把 `head_dim` 列重复到 `tile_N`（`(head_dim, tile_N//head_dim)` 内层、步长 `(stride, 0)`）；否则一个头跨多个 tile，按 `tile_coord % (head_dim//tile_N)` 切片。注意这是**纯布局视图，不搬数据**——编译后只是不同地址计算（呼应 u3-l2 的「改布局 ≠ 搬数据」）。

TMA 路有一个本质限制：**TMA 描述符无法编码 stride-0 的 head 广播**，所以广播被搬进了**拷贝坐标**：

[quack/epilogue/rotary.py:269-277](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/rotary.py#L269-L277) —— 每个 epi subtile 在列 `coord[1] % subtiles_per_head` 处 TMA 加载表盒（跨头的冗余加载命中 L2）。这样整条 `TileLoad` 消费路径（S2R、staging、pipeline tx 记账）原样继承。

posfreq 路用内核内 sincos，关键是把角度算成 **float-float、以「圈」（turns）为单位**以避免精度崩塌：

[quack/epilogue/rotary.py:425-438](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/rotary.py#L425-L438) —— `_angle_turns`：`theta = pos * inv_freq` 表达成未求值的 float-float 和 `(t, lo)`，`lo` 用 FFMA 残差把乘积舍掉的低位带回来。

[quack/epilogue/rotary.py:441-472](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/rotary.py#L441-L472) —— `_sincos_turns`：圈单位让 range reduction 变成精确的 mod-1（一个 round + 减法，无需 Cody-Waite 常数拆分）；round 用「magic-bias add」而非 `FRND`，因为 MUFU.SIN/COS 与 FRND 都走四分频 XU 管（epilogue 的算术瓶颈），全频 FMA 操作更划算。

主机端 `make_interleaved_inv_freq` 把「非旋转布局」编码为**数据**（零频率 = 角度 0 = 恒等旋转）：

[quack/epilogue/rotary.py:573-606](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/rotary.py#L573-L606) —— 打包 QKV 时让最后 `nonrotary_n` 列（V 块）零频率，V 逐位通过；部分旋转（rotary_dim < head_dim）让每头尾部零频率。NTK/YaRN 的频率变换是纯数据：把变换后的 inv_freq 喂进来即可。

#### 4.1.4 代码实践

**实践目标**：验证「表加载 op 的 head 广播」与「packed QKV 的 V 逐位通过」两件事。

**操作步骤**（源码阅读型 + 小调用示例）：

1. 读 [quack/epilogue/rotary.py:660-667](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/rotary.py#L660-L667) 的 `make_interleaved_cos_sin`，确认它把 `(seqlen, head_dim/2)` 的 cos/sin 交错成偶/奇列。
2. 读 [quack/epilogue/rotary.py:172-189](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/rotary.py#L172-L189) 的 `RotaryCosSinLoadHost`，注意 `fn_port = "value"`——这就是让它能作为 `cs` 进入 `rope_table_epi` 函数体的唯一声明。
3. 写一段「示例代码」构造 packed QKV 的频率表（仅说明意图，不运行内核）：

   ```python
   # 示例代码（仅构造主机端表，不启动内核）
   import torch
   head_dim = 128
   inv_freq = 1.0 / (10000 ** (torch.arange(0, head_dim, 2).float() / head_dim))
   # Q/K 各 head_dim 旋转，V 的 head_dim 列零频率（逐位通过）
   from quack.epilogue.rotary import make_interleaved_inv_freq
   freq = make_interleaved_inv_freq(inv_freq, rotary_n=2 * head_dim, nonrotary_n=head_dim)
   # freq 形状 (3*head_dim,)，最后一头全 0 → 角度 0 → cos=1,sin=0 → 旋转为恒等
   ```

**需要观察的现象**：`freq` 的最后 `head_dim` 个元素是否全为 0；解释为何 `MUFU sincos(0)` 恰为 `(0, 1)`，从而 `x1*1 - x2*0 = x1`、`x1*0 + x2*1 = x2`（V 逐位通过）。

**预期结果**：表加载 op 的 stride-0 广播让单次 `partition_for_epilogue` 即可与 `tRS_rD` 逐元素对齐；packed QKV 的 V 块因零频率而无开销通过。

> 待本地验证：在 SM90/SM100 上跑 `tests/test_gemm_epilogue.py::test_epi_mod_rope_posfreq_packed_qkv` 观察数值。

#### 4.1.5 小练习与答案

**练习 1**：为何 `RotaryCosSinTMALoad` 不能像 LDG op 那样用 stride-0 模表达 head 广播？
**答案**：TMA 描述符无法编码 stride-0（零步长）的重复模式；所以广播被搬到「拷贝坐标」`coord[1] % subtiles_per_head`，每次加载表盒、靠 L2 吸收跨头冗余。

**练习 2**：`rope_posfreq_epi` 为何用「圈（turns）」而非弧度做 range reduction？
**答案**：圈单位下 mod-1 是精确的 round+减法，无需 Cody-Waite 常数拆分；且 MUFU.SIN 硬件原生以圈为单位（ptxas 对弧度 `sin.approx` 也会前置乘 1/2π），所以圈是「正确单位」。

---

### 4.2 GroupedColStatsBase 预扫描统计：HeadRstd

#### 4.2.1 概念说明

很多 epilogue 需要「先对原始累加器算一个统计量，再在主存储时逐元素应用它」。典型例子：

- **每头 RMSNorm**：rstd = rsqrt(mean(x²))，主函数里 `D = acc * rstd * w`。
- **稳定 exp 的偏移**：每（行，N-tile）的 max，主函数里 `exp(acc − max)`。

`GroupedColStatsBase` 把这个共性抽成一个 op：统计量按「（tile 行，group of `group_cols` 列）」分组累加，全程**无浮点原子、无逐 subtile smem 流量**，结果留在寄存器里供主函数读取。它同时是 **prepass sink**（预扫描阶段收集统计输入）和 **value port**（主阶段把 finalized 统计值广播成逐行值）。`HeadRstd`（head_rmsnorm.py）是它最小的真实实例——只设 `combine="add"`、定义 `stat_value`。

> 名词解释：**prepass**（预扫描）= 在任何存储之前，对原始累加器跑一遍 `fn2`（这里是 `acc*acc`），把结果喂给 prepass sink op。**group**（分组）= `group_cols` 个连续 N 列对应一个统计值（如 `head_dim` 列对应一个每头 rstd）。

#### 4.2.2 核心流程

1. **`stats_begin`**：构造坐标分区、行广播参考布局、lane/warp 几何、清零的寄存器累加器 `rStats`，并把 smem 各 plane 填为 fold 单位元（add 为 0、max 为 −∞）。
2. **预扫描 sweep**（`fn_sink_flush`）：每个 subtile 把统计输入按静态 `(行, 组序号)` 折叠进 `rStats`，无 smem、无 shuffle。
3. **`fn_prepass_end`**：每个 slot 跨 N-lane 组做蝶形归约；`warps_in_N==1` 时直接 `stat_value` 定稿到 `rStats`；同时把**原始** partial 存一次到 `(行, 组, warp_n)` smem plane。
4. **`fn_prepass_resolve`**（仅 `warps_in_N>1`）：跨 warp 屏障后，每条消费 lane 把各 plane 折叠、`stat_value`、覆盖自己的 `rStats`（无共享写，无需第二屏障）。
5. **主阶段**（`fn_prepare`，value 端口）：把 `rStats` 的逐行 finalized 值广播成与累加器逐元素对齐的片段，主函数里直接 `acc * qk`。

#### 4.2.3 源码精读

`HeadRstd` 全部内容只有 finalize 一行——这正是「最小实例」的教学价值：

[quack/epilogue/head_rmsnorm.py:12-34](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/head_rmsnorm.py#L12-L34) —— `combine = "add"`（继承基类），`stat_value` 把分组平方和 finalize 成 rstd：`rsqrt(total * (1/group_cols) + eps)`。主机参数是 `head_dim`（一个 int，或任何长度为 head_dim 的 1-D 张量，只用其长度固定组宽）。

`stat_value` 是子类唯一必须实现的钩子（基类抽象）：

[quack/epilogue/ops.py:2452-2456](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L2452-L2456) —— 签名 `stat_value(total, group_cols)`，返回逐行 Float32。

预扫描 sink 的「无原子折叠」核心：

[quack/epilogue/ops.py:2358-2374](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L2358-L2374) —— `fn_sink_flush` 用 `convert_layout_zero_stride` 把片段按行重打包，每行的列折叠成一个 partial，再按静态 `(行, 组序号)` 累加进 `rStats`。寄存器索引是编译期静态的（每线程每 subtile 的列 run 恰落在一个组内）。

预扫描结束的「一次蝶形 + 一次 smem 存储」：

[quack/epilogue/ops.py:2377-2407](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L2377-L2407) —— `fn_prepass_end`：跨 N-lane 组蝶形；`warps_in_N==1` 时就地 `stat_value` 定稿；lane 领导把**原始** partial 存到 smem plane（`coord[1] // group_cols` 恢复绝对组号）。

主阶段把统计值广播成逐元素片段：

[quack/epilogue/ops.py:2463-2477](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L2463-L2477) —— `fn_prepare` 读 `rStats[row_base + r, ord_n]`，按广播列填满 `out_mn`。返回的片段与累加器逐元素对齐——主函数里 `acc * qk` 自然成立。

最后看 `qknorm_epi` 如何组装这一切：

[quack/epilogue/library.py:353-366](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/library.py#L353-L366) —— `prepass=_sq_prepass`（返回 `acc*acc`），`ops={"qk": HeadRstd(...)}`（`qk` 既是 prepass sink 又是 value 端口），`extra_ops=(...out("rstd_out"),)`（可选地把 rstd 写到 gmem 供反向用）。函数体 `acc * qk * w` 把 rstd 与权重 `w`（独立 rowvec）相乘，**全在源码里一目了然**。

#### 4.2.4 代码实践

**实践目标**：跟踪 `qknorm_epi` 的一次 prepass → 主阶段数据流，确认 rstd 是「先于存储算好、再逐元素应用」。

**操作步骤**（源码阅读型跟踪）：

1. 读 [quack/epilogue/library.py:326-330](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/library.py#L326-L330) 的 `_sq_prepass`：它返回 `{"qk": acc * acc}`——统计输入是平方值。
2. 跟 `HeadRstd` 的 `combine="add"`：prepass 把 `acc*acc` 按每 `head_dim` 列求和进 `rStats`（无原子）。
3. 跟 [quack/epilogue/head_rmsnorm.py:32-34](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/head_rmsnorm.py#L32-L34)：`stat_value` 把和除以 `group_cols`（=head_dim）再加 eps，rsqrt 得 rstd。
4. 跟 [quack/epilogue/ops.py:2463-2477](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L2463-L2477)：rstd 广播成逐行值，主函数 `acc * qk * w`。

**需要观察的现象**：rstd 的计算发生在主存储（`store_convert`）之前；`rstd_out` 由 `GroupedColStatsOut`（下一节）写一次到 gmem。

**预期结果**：能说清「prepass 在驱动循环里由 `epi_needs_acc_prepass` 标志触发（见 [quack/gemm_base.py:332-353](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L332-L353)），主阶段读寄存器里的 finalized rstd」。

> 待本地验证：跑 `tests/test_gemm_epilogue.py::test_epi_mod_qknorm_rope_prepass` 观察 rstd_out 与 torch 参考一致。

#### 4.2.5 小练习与答案

**练习 1**：为何 `GroupedColStatsBase` 的折叠「无浮点原子」？
**答案**：折叠在**线程私有寄存器** `rStats` 里按静态 `(行, 组序号)` 索引累加；跨 lane/warp 的合并用蝶形 shuffle 和 smem plane（每 slot 单写者），全程不经原子。

**练习 2**：`host_arg_key` 为何对一个 `int` 和一个张量给出不同 key？
**答案**：`int` 是纯编译期组宽，烘焙进 trace、无运行期参数（key `("width", n)`）；张量形式带运行期指针（key `(dtype, length)`）。两种形式生成结构不同的 cubin，必须区分以避免 JIT 缓存别名。

---

### 4.3 scaled_exp / LSE 归约输出

#### 4.3.1 概念说明

`scaled_exp` 解决的是「稳定地存 \(E = \exp(\text{acc})\)，同时吐出重建它所需的 partials」——这是 softmax 分子 / logsumexp 的通用积木（linear-CE 前向的 gemm1 是动机消费者）。朴素 `exp(acc)` 在 logits 很负时会下溢到 0、很正时溢出。稳定做法是减去一个偏移再 exp。

QuACK 的选择是**每（行，N-tile）的幂次偏移**：

\[
E = \exp_2(\text{acc} \cdot \log_2 e - k),\quad k = \mathrm{roundeven}(\max \cdot \log_2 e)
\]

其中 max 是该（行，N-tile）的真 max。用 2 的幂次偏移有三个好处：下游 `2^(k−k_r)` 的缩放是**精确的 bf16 乘法**；RNE（非 ceil）让 \(E \le \sqrt{2}\)（整数性是幂次精确性所需，bf16 的量程足够）；消费者直接读 k，主机侧无需重新推导、无需匹配舍入约定。

这又是一次 `GroupedColStatsBase`（`combine="max"`）+ 主函数 + 归约的组合。同时引入三种「沿 N 输出」的 sink op：`ColVecReduce`、`OnlineLSEReduce`、`ColVecSelect`。

#### 4.3.2 核心流程

`scaled_exp_epi` 两阶段：

1. **Phase 1（prepass）**：`MaxLog2`（`combine="max"`，单位元 −∞）对原始累加器求每（行，N-tile）真 max；`stat_value` 算 `k = roundeven(max * log2e)`。
2. **Phase 2（主函数）**：`e = pexp2(acc*log2e − max_log2)`；返回 `{"D": e, "sum_exp": e}`。`sum_exp` 是 `ColVecReduce("add")`——每（行，N-tile）的 exp 之和；`max_log2_out`（`GroupedColStatsOut`）把定稿的 k 直接从 prepass 统计 smem 写到 gmem（每（行，组）一次存储，逐元素路径零开销）。

LSE 的两种归约 flavor：

- **`ColVecReduce`**（`scaled_exp_epi` 的 sum_exp、library 的 `lse_partial_epi`）：每 N-tile 一个 partial，主机 `.sum`/`logsumexp` finalize。
- **`OnlineLSEReduce`**（library 的 `lse_epi`）：耦合的 (running max, running sum) 累加器——每个新值可能 rescale sum。partial 也是 `(l, m, n_tiles)`，主机 `torch.logsumexp(partials, -1)`。

`ColVecSelect`（library 的 `lse_target_epi`、scaled_exp 的 target 变体）：逐行选一列（gather），如目标 token 的 logit。

#### 4.3.3 源码精读

模块 docstring 把两阶段策略讲得很清楚：

[quack/epilogue/scaled_exp.py:1-23](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/scaled_exp.py#L1-L23) —— Phase 1 是真 max（−∞ 单位元，全负 tile 得负 k——最紧的偏移）；Phase 2 的 k 以 value 端口回喂，主函数存 `exp2(acc*log2e − k)`。

`MaxLog2` 是 `GroupedColStatsBase` 的 max 实例：

[quack/epilogue/scaled_exp.py:35-68](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/scaled_exp.py#L35-L68) —— `combine = "max"`，`stat_value` 把 max 转 log2 单位并（可选）RNE 成整数 k。`config_key = (round_to_int,)`——`round_to_int` 是结构开关，必须进缓存键。

主函数组装：

[quack/epilogue/scaled_exp.py:78-94](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/scaled_exp.py#L78-L94) —— `@gemm_epilogue` 装饰：`ops={"max_log2": _max_log2_op}`（prepass + value）、`prepass=_max_prepass`、`reduces={"sum_exp": ColVecReduce(...)}`、`extra_ops=(_max_log2_op.out("max_log2_out"),)`。函数体 `e = pexp2(acc*LOG2E − max_log2); return {"D": e, "sum_exp": e}`。

`GroupedColStatsOut` 把定稿统计值零开销写到 gmem：

[quack/epilogue/ops.py:2555-2588](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L2555-L2588) —— `end_loop_finish`：选每（行，组）一个写者，折叠兄弟的原始 warp_n plane、`stat_value`、直接写 gmem。这「游离于逐元素热路径之外」——对比把 value 端口路由进 reduce sink（每元素一次 combine）或用 per-tile reduce slot（对 sub-tile 组太粗）。

`OnlineLSEReduce` 的耦合 (max, sum) 折叠——`combine=` 表达不了的：

[quack/epilogue/ops.py:2649-2658](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L2649-L2658) —— `_merge`：新值到来时按全局 max rescale 两侧 sum。公式 \(s_{new} = s\,e^{m-m_{new}} + o_s\,e^{o_m-m_{new}}\) 是耦合传输的数学基础（呼应 u2-l4 的 online softmax）。

[quack/epilogue/ops.py:2706-2754](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L2706-L2754) —— `_fold`：先线程片段 max（FMNMX 树，无 exp），再一次 rescale running sum + 每元素一次 exp（把朴素 online 每元素两次 exp 砍半；MUFU.EX2 四分频管是折叠的墙）。

`ColVecSelect` 的逐行列选取（target logit）：

[quack/epilogue/ops.py:2814-2871](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L2814-L2871) —— `out[m]` = 行 `idx[m]` 列处的值。索引由伴生 `ColVecLoad`（int32/int64）staging，函数体看不到；从 smem 读 idx、用 rebased-compare 与静态逐元素 N 偏移比。整张 (M,N) 网至多一个元素满足 `col == idx[row]`，故无折叠、无 lane/warp 交换、无屏障——持有命中元素的线程谓词存储。

#### 4.3.4 代码实践

**实践目标**：对比 `scaled_exp_epi`（显式 pow2 偏移 + ColVecReduce sum_exp）与 `lse_epi`（OnlineLSEReduce 耦合累加）两条 LSE 路径。

**操作步骤**（源码阅读型对比）：

1. 读 [quack/epilogue/scaled_exp.py:78-94](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/scaled_exp.py#L78-L94)：偏移 `max_log2` 由 prepass 提供，sum_exp 是普通 add 归约。
2. 读 [quack/epilogue/library.py:279-283](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/library.py#L279-L283)：`lse_epi` 用 `outs={"lse": OnlineLSEReduce(...)}`，函数体只 `return {"D": acc, "lse": acc}`——数值稳定性由 op 拥有，函数体不操心。
3. 读 [quack/epilogue/ops.py:2618-2619](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L2618-L2619)：`OnlineLSEReduce.host_finalize = torch.logsumexp(partials, -1)`。

**需要观察的现象**：`scaled_exp_epi` 把「减 max」显式写在函数体（`acc*LOG2E − max_log2`），而 `lse_epi` 的稳定性藏在 `OnlineLSEReduce._fold` 的耦合累加里——两种风格，函数体都是「组合点」。

**预期结果**：能说清「`scaled_exp` 用幂次偏移使下游缩放精确；`OnlineLSEReduce` 用耦合 (max,sum) 把稳定性内化到归约 op，函数体保持极简」。

> 待本地验证：跑 `tests/test_gemm_epilogue.py::test_epi_mod_online_lse` 与 `test_epi_mod_lse_partials` 比对两种 LSE 输出。

#### 4.3.5 小练习与答案

**练习 1**：为何 `MaxLog2` 的 `round_to_int` 默认 True，且用 RNE 而非 ceil？
**答案**：整数 k 让下游 `2^(k−k_r)` 是精确 bf16 乘法；RNE（非 ceil）让 \(E \le \sqrt{2}\)——整数性是幂次精确性所需，bf16 量程足够吸收这个范围。

**练习 2**：`lse_target_epi` 里 target logit 为何不通过 value 端口路由索引？
**答案**：注释的实测「墓碑」记录：经 value 端口会把广播索引片段逐元素物化、转 f32、收集进第二个 sink plane——开销数倍于从伴生 smem 直接读 idx 的 `ColVecSelect` 方案。

---

### 4.4 quantize_out 量化输出（BlockScaleFactorStore）

#### 4.4.1 概念说明

`BlockScaleFactorStore`（SFD op）把最终 D（或 aux postact）量化成 fp8/fp4，并写出 blocked scale factors。机制是 **per SF vector 算 amax → 量化 scale → 原地 rescale 累加器片段**，让随后普通的 `f32 → d_dtype` 存储自然产出量化值。

SF vector 的粒度由格式定：e8m0 scale（mx 格式 mxfp8/mxfp4）覆盖 **32** 个值，e4m3 scale（nvfp4）覆盖 **16** 个值，沿 N 连续（`sf_vec_size_for`）。量化公式：

\[
\text{scale} = \frac{\text{amax}}{d_{\max}} \cdot \text{norm\_const},\qquad
\text{rescale} = \min(\text{norm\_const} \cdot \text{rcp}(\text{dequant}(\text{SF})),\; \text{FLT\_MAX})
\]

其中 \(d_{\max}\) 是值 dtype 的最大可表示值（fp8e4m3=448、fp4e2m1=6）。SF 的 f32→e8m0 向上取整（round toward +inf，硬件 cvt 语义），f32→e4m3 RNE——与 cuBLAS / CUTLASS C++ `Sm100BlockScaleFactorRowStore` 逐位一致。

方向是参数：`"row"`（默认，SF 向量沿 N，输出喂下一个 GEMM 的 K）与 `"col"`（沿 M，给反向消费者）。两方向在 SM100 tmem epilogue 与 SM90-style 寄存器 epilogue（SM120 warp MMA）上都跑。

#### 4.4.2 核心流程

1. **`to_params`**：把 blocked `(L, rm, rk, 32, 4, 4)` SF 张量 re-view 成逻辑 `(M_pad, N_pad, L)`，其向量内模 stride 为 0（可像 D 一样 tile/partition）；校验 arch∈(100,120)、sf_dtype∈(e8m0,e4m3)、fp32 累加等。
2. **`begin`**：建 gmem 视图；构造 **SF-slot 寄存器张量**（零步长广播布局——VecReduce 的把戏，`vec` 元素别名一个 slot）；从 tiled_copy 推 lane/warp 几何，算 `lane_span`/`warp_span`（一个 SF 向量可能跨 lane 甚至跨 warp）。
3. **驱动 store 循环**（`gemm_base.epilogue`）对每个存储输出调用 `quant.quantize(...)`，**在 `store_convert` 之前**对最终 fragment 原地量化。
4. **`quantize`** → `sfd_quantize_subtile`：逐元素 `fmax(..., abs=True)` 累加进 amax slot → lane 蝶形 →（SM120 跨 warp 时）`sExch` smem 交换 → `quantize_sf_slots` 生成 SF 字节 + rescale 因子 → 原地 rescale `tRS_rD`。
5. **`store_convert`**：被 rescale 过的 fragment 经普通 `f32 → d_dtype` 转换，自然产出量化值。
6. **`end`**：SF 字节由领导 lane/warp 以打包 u32/u16 向量化存到 gmem。

#### 4.4.3 源码精读

`quantize` 是驱动调用的入口——时机决定一切：

[quack/epilogue/quantize_out.py:682-716](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/quantize_out.py#L682-L716) —— 注释点明：驱动 store 循环在「`epi_visit_subtile` 和 `epi_end_loop` 之后、拥有者 store op 的 storage-dtype convert 之前」调用它。所以所有未 rescale 值的消费者都已跑完，原地 rescale 后 convert 直接出量化值。

原地量化的核心循环：

[quack/epilogue/quantize_out.py:834-941](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/quantize_out.py#L834-L941) —— `sfd_quantize_subtile`：零步长广播布局让每元素累加进自己的 SF slot（与片段寄存器顺序无关）；非 SM100 用 `fmax(..., abs=True)`（xorsign）保幅值同时 XOR 符号、省去逐元素 absf，slot 末尾清一次符号；SM100 保留 `fmax(acc, |x|)` 链（ptxas 融成 3 输入 `FMNMX3.ABS`，SM100 专属）。随后 `quantize_sf_slots` 与原地 `tRS_rD[i] = tRS_rD[i] * tDrScale[...]`。

共享的量化核心（与融合 RMSNorm/LayerNorm 量化前向共用）：

[quack/blockscaled/quantize_utils.py:49-87](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/blockscaled/quantize_utils.py#L49-L87) —— `quantize_sf_slots`：`scale = amax * norm_scaled`；e8m0 时 rescale = `pow2(254 − byte)`（精确），否则 `rcp_approx`。注释强调全程标量 setitem——因为全零步长 filtered 视图上的 `TensorSSA` store 会被 DSL 静默丢弃（呼应 CLAUDE.md 的 gotcha）。

SF 字节的 gmem 写出（`end`）：

[quack/epilogue/quantize_out.py:783-831](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/quantize_out.py#L783-L831) —— lane-split 时每 lane 持相同 SF 字节，仅子组领导存；warp-split 时仅 member 0 存。整 tile autovec flush 仅当 `begin` 已 CTA-tile 了 gmem 视图（row 方向、`tile_N % (4*vec) == 0`），否则 per-subtile 带边界检查 flush。

驱动 store 循环把 codec 接到每个存储输出上：

[quack/gemm_base.py:439-453](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L439-L453) —— 对每个 store context：若 `quant is not None`，先 `quant.quantize(self, epi_loop_tensors[quant.name], store_frags[i])`，再 `op.store_convert(...)`。`store_frags[i]` 是该输出的最终 fragment（D 或 aux）。

codec 如何按输出名解析：

[quack/epilogue/mixin.py:177-185](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/mixin.py#L177-L185) —— `_epi_store_quant(output_name)`：遍历激活 op 集，找 `quant_output == output_name` 的那个。`"D"` 是主输出；不传 SF 张量则该 op 被过滤出编译产物（什么都不量化）。

最后看 aux（postact）量化如何声明——`BlockScaleFactorStore` 挂在 `TileStore` 的 `quant=` 字段上：

[quack/epilogue/library.py:216-231](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/library.py#L216-L231) —— `gated_quant_mod`：`TileStore("postact", gated=True, quant=BlockScaleFactorStore("postact_sf", output="postact"))`。前端把 quant codec 提升进 `extra_ops`，驱动在其拥有者 postact 的最终 fragment 上跑它。

#### 4.4.4 代码实践（本讲指定实践）

**实践目标**：说清 `BlockScaleFactorStore` 如何在 store 前对最终 D fragment 原地量化、写出 blocked SF，并指出它与 `DStore` 的协作。

**操作步骤**（源码阅读型 + 测试参考）：

1. **读驱动接缝**：[quack/gemm_base.py:439-453](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L439-L453)。确认顺序是 `quant.quantize(frag)` → `op.store_convert(frag)` → `op.store_r2s`。`quantize` 原地改 `frag`，故 `store_convert` 的 `f32→d_dtype` 出量化值。
2. **读 codec 入口**：[quack/epilogue/quantize_out.py:682-716](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/quantize_out.py#L682-L716)。`quantize` 按 arch 分派 `sfd_quantize_subtile`（row / SM120 col）或 `sfd_quantize_subtile_col`（SM100 col redux）。
3. **读量化核心**：[quack/blockscaled/quantize_utils.py:49-87](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/blockscaled/quantize_utils.py#L49-L87)。手算：amax=2.0、fmt=mxfp8_e4m3（\(d_{\max}=448\)）→ scale=2/448≈4.46e-3 → e8m0 字节 = 向上取整的指数 → rescale = 2^((254−byte) 的 e8m0 解码)。
4. **协作关系**：对比 [quack/epilogue/ops.py:1111-1158](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L1111-L1158) 的 `DStore`。`DStore` 拥有 convert（kernel 全局舍入、SR seed）与 r2s，但**无主机钩子、不在 `_epi_ops`**——D 的主机管线（TMA atom、staged smem、`sD` 字段、split-K 工作区重指向）由内核拥有。`BlockScaleFactorStore` 不重复 convert/r2s，只在 convert **之前**插一脚原地 rescale，二者共享同一套 `store_convert`/`store_r2s` store 钩子。
5. **测试参考**：[tests/test_gemm_quant_out.py:110-126](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/test_gemm_quant_out.py#L110-L126) 的 `test_quant_out_exact`：B=单位阵 ⇒ D==A 逐位精确，于是 SF 字节与 D 值都必须与参考 `quant_ref` 逐位一致。

**需要观察的现象**：`gemm(..., out_dtype=fmt)` 返回 `BlockScaledOperand`，含 `.qdata`（量化值）与 `.scale`（blocked SF）；SF 字节与参考逐位相等（exact 测试）。

**预期结果**：能复述「`BlockScaleFactorStore.quantize` 在 `store_convert` 前原地 rescale；`DStore` 拥有 convert/r2s；二者经驱动 store 循环的统一 `(op, quant, ...)` context 组合，codec 由 `_epi_store_quant(output_name)` 解析」。

> 待本地验证：需 SM100/SM120。跑 `pytest tests/test_gemm_quant_out.py::test_quant_out_exact -x`。

#### 4.4.5 小练习与答案

**练习 1**：为何 SF slot 张量用零步长广播布局？
**答案**：让 `vec` 个逻辑元素别名同一个 SF slot，从而 amax 累加与片段寄存器顺序无关——`filter_zeros` 两边后 slot 顺序自动匹配（VecReduce 的同款把戏，u6-l2）。

**练习 2**：SM100 col 方向为何有独立的 `redux.sync` 路径？
**答案**：SM100 tmem-load 几何下 `lane == row`，每列的 amax 是一条 `redux.sync.max.abs.NaN.f32`（SM100 族指令），SF 字节逐 subtile 内联存储——无需通用 slot 机制的 smem 交换（见 [quack/epilogue/quantize_out.py:205-216](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/quantize_out.py#L205-L216)）。SM120 warp-MMA 片段把每列摊到 stride-4 lane 组与 16 行 warp 条，故走通用蝶形 + `sExch` 路径。

---

## 5. 综合实践

把本讲三个主题串成一个真实算子：**带每头 RMSNorm + RoPE 的 QK 投影**。这正是 `library.py` 里的 `qk_rope_epi`。

1. 读 [quack/epilogue/library.py:369-386](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/library.py#L369-L386) 的 `qk_rope_epi`：它用 `ops={"cs": rotary_cos_sin_load("cs"), "qk": HeadRstd(...), "w": RowVecLoad("w")}`，`prepass=_sq_prepass`，`mode="acc_pair"`。
2. 函数体 `x1, x2 = unpack(acc * qk * w); c, s = unpack(cs); return {"D": pack(x1*c - x2*s, x1*s + x2*c)}`。
3. **串讲**：
   - **预扫描统计**（4.2）：prepass 对 acc 求每头平方和，`HeadRstd.stat_value` 定稿 rstd，主阶段以 value 端口 `qk` 广播。
   - **rotary**（4.1）：`cs` 是 cos/sin 表 op 的 value 端口，TMA 加载、head 广播靠拷贝坐标。
   - **组合**：`acc * qk * w`（归一化+权重）后再 `unpack` 成对、旋转、`pack` 写回。计算顺序（先 rstd*w 再旋转）在源码里显式、可审阅。

任务：在 `qk_rope_epi` 的函数体里，如果把 `acc * qk * w` 改成 `(acc * w) * qk`，数值是否改变？为什么？（提示：`qk` 是逐行 rstd，`w` 是逐列权重，二者广播维度不同；乘法可交换但顺序对精度无影响——不过若 rstd 延迟到下一个 GEMM（如 `rms_partial_epi`），则 rstd 必须在该 GEMM 之后应用。）

> 待本地验证：跑 `tests/test_gemm_epilogue.py::test_epi_mod_qknorm_rope_prepass`，对比 `qk_rope_epi` 与「先 rstd*w 再 rope」的 torch 参考是否逐位一致。

## 6. 本讲小结

- **rotary** 把「cos/sin 从哪来」封装成一个 `value` 端口 op：表加载（TMA 默认，head 广播靠拷贝坐标）或内核内 sincos（float-float turns 数学）；非旋转布局（packed QKV 的 V、部分旋转）编码为**零频率数据**。
- **`GroupedColStatsBase`** 抽象了「主存储前算分组统计、再逐元素应用」：预扫描 sink（无原子折叠进寄存器）+ value 端口（广播 finalized 值）。`HeadRstd`（rsqrt(mean+eps)）是其最小实例。
- **scaled_exp** 用每（行，N-tile）的幂次偏移做稳定 exp 存储：`MaxLog2`（max prepass）+ 主函数 `exp2(acc*log2e − k)` + `ColVecReduce` sum_exp + `GroupedColStatsOut` 零开销写 k。LSE 另有 `OnlineLSEReduce`（耦合 max,sum 累加器）与 `ColVecSelect`（逐行 target gather）。
- **`BlockScaleFactorStore`** 在驱动 store 循环的 `store_convert` **之前**对最终 fragment 原地量化：amax → `quantize_sf_slots` → rescale，使普通 convert 出量化值；SF 字节由领导 lane/warp 打包写出。它与 `DStore`（拥有 convert/r2s、无主机钩子）共享 store 钩子，由 `_epi_store_quant(output_name)` 解析。
- 贯穿全讲的心法：**领域 op = 新 `EpiOp` + 一个端口方法**。函数体是组合点（计算顺序显式），op 是扩展点（资源生命周期写一次即可与所有其它 op 组合）。

## 7. 下一步学习建议

- **向下到设备侧**：`BlockScaleFactorStore` 的 row/col 几何依赖各 SM 的 epilogue warp 形状与 tiled_copy，建议接着读 u5-l3（SM100 TMEM epilogue）与 u5-l4（SM120 warp MMA 的 `mma_n_warp_run` 加宽），理解 SM100 redux 路径与 SM120 slot+`sExch` 路径的几何来源。
- **横向到量化输入**：`quantize_out` 写出的 SFD 布局与输入侧 SFA/SFB 的 blocked 布局共享——下一单元 u7-l1（Blockscaled 操作数与格式）会讲 MXFP8/NVFP4/MXFP6 的输入侧。
- **深入测试方法**：`test_gemm_epilogue.py` 对每个 mod 都有 bitwise-or-1-ulp + ≤1% 性能的对拍；u8-l5 会讲这种「vs PyTorch 参考」的数值正确性测试范式。建议挑 `test_epi_mod_online_lse` 或 `test_quant_out_exact` 跟读一遍参考实现。
