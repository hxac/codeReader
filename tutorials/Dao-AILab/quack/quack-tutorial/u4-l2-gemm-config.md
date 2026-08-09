# GemmConfig 配置空间

## 1. 本讲目标

GEMM 是 QuACK 里最庞大的子系统。在真正进入「主机侧编译缓存」(u4-l1) 和「设备侧各架构内核」(u5) 之间，我们需要先认识一个贯穿两者的「数据契约」——`GemmConfig`：它用一组标量字段描述「这次 GEMM 要切成多大的块、用几个 CTA 协作、要不要 pingpong、沿 K 维要不要切」。自动调优器、剪枝器、默认配置选择器、blockscaled 量化路径，全都围着它转。

读完本讲你应当能够：

1. 说出 `GemmConfig` 每个字段（`tile_m/tile_n/tile_k`、`cluster_m/n/k`、`pingpong`、`is_dynamic_persistent`、`swap_ab`、`split_k` 等）的物理含义。
2. 理解 `SplitKMode` 三种模式（SERIAL / PARALLEL / SEPARATE）在「合并方式」与「确定性」上的取舍。
3. 看懂 `_get_sm80/90/100/120_configs` 如何用 `itertools.product` 笛卡尔积生成各架构的调优空间，并能解释为什么 **SM100 没有 pingpong**。
4. 说明 `default_config` 如何按 `device_capacity` 选默认值，以及 `blockscaled_config_ok` / `cta_tile_shape_m` 这两个约束函数的作用。

## 2. 前置知识

本讲默认你已经掌握 u1-l3（目录结构与模块地图）和 u1-l4（CuTe-DSL 编程模型）。下面几个概念会反复出现，先建立直觉：

- **tile（瓦片）**：GEMM 把大矩阵切成小块逐块计算，每块叫一个 tile。`tile_m × tile_n` 是一个 CTA（线程块）一次负责的输出块大小；`tile_k` 是一次累加的 K 维步长。tile 越大，单块算得越多、循环开销越小，但占用的寄存器 / 共享内存越多。
- **cluster（集群）**：Hopper 起引入的概念，若干个 CTA 编为一组（如 `(2,1)` 表示 M 方向 2 个 CTA），它们能通过集群共享内存协作、做 TMA 多播。`cluster_m/n/k` 就是集群在三个维度上的 CTA 数。
- **CTA / SM**：CTA 是「线程块」（CUDA 里 launch 的单位）；SM 是芯片上真正跑 CTA 的硬件单元（流多处理器）。`device_capacity` 用 SM 的主版本号标识架构（`8`=SM8x/Ampere，`9`=SM90/Hopper，`10`=SM100/Blackwell 数据中心，`11`=SM110，`12`=SM120/Blackwell 消费级）。
- **pingpong（乒乓）**：一种让两组 MMA warpgroup 交替处理不同 tile、从而把「上一块的 epilogue 存储」与「下一块的 MMA 计算」重叠起来的主循环策略（详见 4.2）。
- **blockscaled（分块量化）**：FP8/FP4 这类低精度格式把每 16 或 32 个元素共享一个 scale factor（缩放因子），QuACK 用 `SFA/SFB` 张量携带这些 SF。详见 u7。
- **持久化内核 / 动态调度**：内核常驻 SM、自己从调度器领 tile，而不是硬件一次性把 tile 派发给所有 CTA。`is_dynamic_persistent=True` 走硬件 CLC（Cluster Launch Control）工作偷取，详见 u3-l4。

> 本讲只讲「配置数据结构」和「配置空间生成」，不涉及任何 GPU 运行。`gemm_config.py` 是一个**纯 Python 叶子模块**（只 import `itertools`/`enum`/`dataclasses` 等），因此本讲的代码实践**不需要 GPU**，只要装好 quack（`pip install -e '.[dev]'`）即可运行。

## 3. 本讲源码地图

本讲几乎只围绕一个文件展开，它被自动调优层和主机接口层反复 import：

| 文件 | 作用 |
|------|------|
| [quack/gemm_config.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_config.py) | **本讲主角**。定义 `SplitKMode` 枚举、`GemmConfig` 数据类、各 SM 的配置空间生成器、默认配置与 blockscaled 约束函数。 |
| [quack/gemm_interface.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_interface.py) | 从 `gemm_config` 再导出全部公共符号（`GemmConfig`/`SplitKMode`/`default_config`/…），并在 `prune_invalid_gemm_configs`、`gemm_tuned` 里消费它们。 |
| [quack/gemm_runtime/autotune.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_runtime/autotune.py) | 可组合 epilogue 的通用调优路径，同样调用 `get_all_configs`/`config_supports`/`blockscaled_config_ok` 做剪枝。 |
| [quack/gemm_sm90.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py) / [gemm_sm100.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm100.py) | 设备侧内核类，是理解「为何 SM100 没有 pingpong」的依据（`GemmSm90.__init__` 有 `pingpong` 参数，`GemmSm100.__init__` 没有）。 |

## 4. 核心概念与源码讲解

本讲分三个最小模块：①`GemmConfig` 数据类与 `SplitKMode` 枚举；②各 SM 配置空间的生成；③默认配置与 blockscaled 约束。

### 4.1 GemmConfig 数据类与 SplitKMode 枚举

#### 4.1.1 概念说明

`GemmConfig` 是一个**不可变的数据类**（`@dataclass(frozen=True)`），用一组标量字段完整描述「这次 GEMM 的几何与策略」。它被用在两个地方：

- 作为**自动调优的一个候选**：调优器枚举大量 `GemmConfig`，逐个跑、挑最快的（u8-l1）。
- 作为**手工指定的固定配置**：用户可把某个 `GemmConfig` 传给 `gemm(...)` 的 `config=` 参数，跳过调优直接用。

`SplitKMode` 是一个 `IntEnum`，描述「当 GEMM 沿 K 维切成多段（split_k > 1）后，各段的 partial 结果如何合并」。它与 `GemmConfig.split_k`（切几段）是配合关系。

为什么 `SplitKMode` 用 `IntEnum`？因为它的值要跨两道边界：①`torch.library` 自定义算子的 schema 只认普通 int；②它要 pickle 进 jit-cache 的缓存键。`IntEnum` 在这两处都表现为普通整数，省去转换。

#### 4.1.2 核心流程

`GemmConfig` 字段可分四组理解：

```
几何（tile / cluster）
  tile_m, tile_n, tile_k   一个 CTA 负责的输出块尺寸（K 步长可省略）
  cluster_m, cluster_n, cluster_k  集群各维 CTA 数（Hopper+ 才有意义）

主循环策略
  pingpong                 True=两组 warpgroup 交替 tile（SM90/SM120 才有）
  num_warps                SM80 用：每个 CTA 的 warp 数（其它架构由 tile 推导）
  is_dynamic_persistent    True=走硬件 CLC 动态调度；False=静态/硬件派发

布局与调度细节
  swap_ab                  True=交换 A、B 角色（D^T = B^T A^T），影响 tile 归属
  max_swizzle_size         L2 swizzle 的 group 上限（见 u3-l4）
  use_tma_gather           仅 SM100/110：gather_A 时用 TMA gather 而非 cp.async

切分与架构标签
  split_k                  沿 K 切几段（1=不切）
  device_capacity          架构主版本号（8/9/10/11/12），决定走哪个内核类
```

`SplitKMode` 三种模式的取舍（核心是「确定性 vs 延迟」）：

| 模式 | 合并方式 | 确定性 | 备注 |
|------|----------|--------|------|
| `SERIAL` (=0) | 按切分顺序，最后一个 split 跑完整 epilogue（旋转门 turnstile） | ✅ 逐位确定 | 默认值 |
| `PARALLEL` (=1) | 各 split 抢着提交，到达计数器填满后 finalize | ❌ 不确定 | 延迟最低 |
| `SEPARATE` (=2) | 每 split 各写自己的 f32 partials，再起一个独立归约内核 | ✅ 确定 | 需额外内核，详见 u8-l3 |

切分 K 的动机是**占用率**：当输出 tile 数（`ntiles = ⌈M/tile_m⌉ × ⌈N/tile_n⌉`）少于 SM 总数时，部分 SM 空闲；沿 K 切能让一个输出 tile 由多个 CTA 分担，把空闲的 SM 用起来。切分后必须把多段结果合并，这就是 `SplitKMode` 解决的问题。

#### 4.1.3 源码精读

先看 `SplitKMode` 枚举定义，注意它的 docstring 解释了为何这个定义必须放在叶子模块 `gemm_config` 而不是 `gemm_interface`：内核层在 import 期就要用它，而 `gemm_interface` 在导入图中位于 `quack.gemm` 之上，放那里会造成循环导入。

[quack/gemm_config.py:L9-L33](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_config.py#L9-L33) —— `SplitKMode` 三个枚举值与各自的合并语义注释。

接着是主角 `GemmConfig`，注意 `frozen=True`（不可变，可哈希、可作缓存键）和各字段的默认值（如 `pingpong=True`、`is_dynamic_persistent=True`、`cluster_m=2` 是「一个通用默认」，但各 SM 生成器都会显式覆盖）：

[quack/gemm_config.py:L36-L54](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_config.py#L36-L54) —— `GemmConfig` 全部 14 个字段，注释标明「SM100 默认开动态持久化、SM90 默认关」。

在主机接口层，`gemm(...)` 的参数列表里 `split_k_mode: int = SplitKMode.SERIAL`（见 [quack/gemm.py:L424](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L424)），就是把枚举当 int 用；而在 `_build_gemm_plan` 里又会 `SplitKMode(split_k_mode)` 把 int 转回枚举（见 [quack/gemm.py:L731-L736](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L731-L736)），并校验 `split_k > 1` 时不能是 blockscaled + SEPARATE。

#### 4.1.4 代码实践

**目标**：确认 `GemmConfig` 的不可变性与 `SplitKMode` 的 int 身份。

**步骤**：

1. 在装好 quack 的环境里（无需 GPU）打开 Python：
   ```python
   from quack.gemm_config import GemmConfig, SplitKMode
   c = GemmConfig()                      # 全默认
   print(c.tile_m, c.tile_n, c.pingpong, c.device_capacity)  # 128 192 True 9
   try:
       c.tile_m = 256                    # frozen，应抛 FrozenInstanceError
   except Exception as e:
       print("frozen:", type(e).__name__)
   print(int(SplitKMode.PARALLEL))       # 1 —— IntEnum 当 int 用
   print({m.name: m.value for m in SplitKMode})
   ```

**预期**：`c` 打印默认值；修改字段报 `FrozenInstanceError`；`SplitKMode` 三个值是 `{'SERIAL': 0, 'PARALLEL': 1, 'SEPARATE': 2}`。（具体默认值以本地运行为准；若 `pip` 版本不同字段默认值可能有微调，**待本地验证**。）

#### 4.1.5 小练习与答案

**Q1**：为什么 `GemmConfig` 要用 `frozen=True`？
**答**：不可变对象可哈希，能作为 `_gemm_plan_cache` / 调优缓存的键；且冻结后可安全地在多线程编译池里共享，避免意外修改。

**Q2**：`split_k=4` 且要求逐位确定的输出，应选哪个 `SplitKMode`？
**答**：`SERIAL`（默认）或 `SEPARATE` 都确定；若要最低延迟且不在乎不确定性，才选 `PARALLEL`。

---

### 4.2 各 SM 配置空间的生成

#### 4.2.1 概念说明

自动调优要有一个「候选池」去搜索。`get_all_configs()` 就是这个池：它把四代架构（SM80/90/100/120）的候选 `GemmConfig` 全部生成出来，每个 config 都自带 `device_capacity` 标签。运行时调优器会先按真实 GPU 的 `device_capacity` 过滤，只测本架构的候选。

这种「一次性生成全部、按标签过滤」的设计有一个好处：**import 期不查 GPU、不初始化 CUDA context**（见 `get_all_configs` 的 docstring）——这在冷启动和无 GPU 的交叉编译场景下很重要。

#### 4.2.2 核心流程

每个 `_get_smXX_configs` 的套路一致：

```
1. 列出本架构支持的 (tile_m, tile_n) 组合，区分 coop 与 pingpong
2. 列出本架构支持的 cluster 组合
3. 列出 swap_ab 取值（True/False，部分 epilogue 下强制 False）
4. 用 itertools.product 做笛卡尔积，每个组合构造一个 GemmConfig
5. 把 device_capacity、is_dynamic_persistent、use_tma_gather 等架构常量写死
```

四代架构的关键差异（这是本讲的核心对比）：

| 架构 | tile 来源 | cluster | pingpong | dynamic_persistent | use_tma_gather |
|------|-----------|---------|----------|--------------------|-----------------|
| SM80 | 固定 8 组 `(m,n,warps)`，tile_k∈{32,64} | `(1,1)` 固定 | ❌ 恒 False | ❌ 恒 False | ❌ 不支持 |
| SM90 | coop(256×N) + pingpong(128×N,192×128) | `(1,2),(2,1)` | ✅ True/False 两种 | ❌ 默认 False | ❌ 不支持 |
| SM100 | 128/256 × {16…256,512}，cluster 四种 | `(1,1)(1,2)(2,1)(2,2)` 等 | ❌ **恒 False** | ✅ True/False（CLC） | ✅ True/False |
| SM120 | coop + pingpong 的小 tile 集 | `(1,1)` 固定 | ✅ True/False | ✅ 恒 True | ❌ 不支持 |

**为什么 SM100 没有 pingpong？** 这是本讲的重点，留给 4.2.3 的源码与设备侧对照来回答。先给结论：SM90 的 pingpong 是用「两组 warpgroup 交替 tile」来把寄存器 epilogue 与 WGMMA 重叠；而 SM100 改用 `tcgen05.mma`（累加器写到专用 **TMEM 张量内存**而非寄存器）+ 专用 warp 分工（加载 warp / MMA warp / epilogue warp）+ 异步 TMEM 流水线，MMA→epilogue 的重叠由硬件异步通路原生提供，不再需要寄存器乒乓这一软件技巧。因此 `GemmSm100.__init__` 根本没有 `pingpong` 参数，对应 `_get_sm100_configs` 把 `pingpong=False` 写死，转而把 `use_clc`（CLC 动态调度）和 `use_tma_gather` 作为可调维度。

#### 4.2.3 源码精读

先看 SM90 的生成器，注意它把 `(m,n,coop)` 与 `(m,n,pingpong)` 分两组，再用 `cluster` 与 `swap_ab` 做笛卡尔积；`is_dynamic_persistent=False`、`use_tma_gather=False` 是 SM90 的写死策略：

[quack/gemm_config.py:L115-L160](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_config.py#L115-L160) —— `_get_sm90_configs`：coop 用 256 行大 tile，pingpong 用 128/192 行小 tile（pingpong 会把 MMA warp 数减半，所以 tile 相应缩小）。

对比 SM100 的生成器，关键三行：`partial(GemmConfig, pingpong=False, device_capacity=10)` 写死无 pingpong；`use_clc_vals=[True,False]` 和 `use_tma_gather_vals=[True,False]` 才是 SM100 真正的调优维度：

[quack/gemm_config.py:L196-L231](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_config.py#L196-L231) —— `_get_sm100_configs`：`# There's no pingpong on Sm100` 一句注释直接点题。

为了坐实「SM100 无 pingpong 是架构决定」，对照两个设备侧内核类的构造函数签名：

- [quack/gemm_sm90.py:L152-L190](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py#L152-L190) —— `GemmSm90.__init__` 接受 `pingpong: bool = False`；其 docstring（[L84-L94](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py#L84-L94)）写明「cooperative: all on one tile; pingpong: two warpgroups alternate tiles」。
- [quack/gemm_sm100.py:L209-L229](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm100.py#L209-L229) —— `GemmSm100.__init__` **没有 `pingpong` 参数**，取而代之的是 `use_clc_persistence: bool = True`（CLC 动态调度）；类 docstring（[L88](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm100.py#L88)）写明用 `tcgen05.mma`（含 2cta mma）。

最后是汇总入口 `get_all_configs`，它把四代拼接返回，docstring 强调「每个 config 自带 device_capacity，调用方运行时再过滤，避免 import 期查设备」：

[quack/gemm_config.py:L263-L278](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_config.py#L263-L278) —— `get_all_configs` 拼接四代架构的候选池。

#### 4.2.4 代码实践（本讲主实践任务）

**目标**：对比 `_get_sm90_configs` 与 `_get_sm100_configs`，用运行结果验证「SM100 无 pingpong」，并理解为何如此。

**步骤**：

1. 运行下面这段**无需 GPU** 的脚本（`get_all_configs` 是纯 Python）：
   ```python
   from quack.gemm_config import get_all_configs

   cfgs = get_all_configs()
   print("总候选数:", len(cfgs))
   for cap in (8, 9, 10, 12):
       sub = [c for c in cfgs if c.device_capacity == cap]
       pp = sum(1 for c in sub if c.pingpong)
       dp = sum(1 for c in sub if c.is_dynamic_persistent)
       tg = sum(1 for c in sub if c.use_tma_gather)
       print(f"cap={cap}: 共 {len(sub):3d} 个, pingpong={pp:3d}, "
             f"dynamic_persistent={dp:3d}, use_tma_gather={tg:3d}")
   ```

2. **观察现象**：
   - `cap=10`（SM100）那一行 `pingpong=0`，而 `cap=9`（SM90）、`cap=12`（SM120）都有非零 pingpong 候选。
   - SM100 的 `dynamic_persistent` 和 `use_tma_gather` 各约占一半（这两个才是它的调优维度）。

3. **解释 SM100 为何没有 pingpong**：打开 [gemm_sm100.py:L209-L229](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm100.py#L209-L229) 确认 `GemmSm100.__init__` 无 `pingpong` 形参，再读类 docstring（[L88](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm100.py#L88)）确认它用 `tcgen05.mma`。结论：Blackwell 的 tcgen05 MMA 把累加器放进专用 TMEM 并配合 warp 分工异步流水线，MMA↔epilogue 重叠由硬件原生提供，故无需 SM90 那种「两组 warpgroup 交替 tile」的寄存器乒乓。

**预期结果**（按源码手算，可本地验证）：总数 486；各架构大致为 cap=8 → 32，cap=9 → 44，cap=10 → 392，cap=12 → 18；其中 cap=10 的 `pingpong=0`。若你本地数字不同，说明源码版本有差异，以本地为准。

#### 4.2.5 小练习与答案

**Q1**：`_get_sm100_configs` 里 `use_clc_vals=[True, False]` 会被映射到 `GemmConfig` 的哪个字段？为什么 SM90 没有这个维度？
**答**：映射到 `is_dynamic_persistent`。SM90 的生成器把 `is_dynamic_persistent=False` 写死（[L152](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_config.py#L152)），因为 Hopper 默认不开 CLC 动态调度（动态调度在 SM90 上需要额外的 gmem 信号量，且收益不如 Blackwell 稳定）。

**Q2**：`epilogue in ["gated"]` 时，SM90 配置空间会做怎样的裁剪？为什么？
**答**：见 [L126-L128](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_config.py#L126-L128)：只保留 `n % 32 == 0` 的 tile，且 coop 去掉 `m==192`。因为 gated epilogue 要把 gate/up 两路在 N 维交错拼接（concat），需要 N 维对齐到 32；且 `swap_ab` 也被强制为 `[False]`（[L140-L141](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_config.py#L140-L141)）。

---

### 4.3 默认配置、blockscaled 约束与 cta_tile_shape_m

#### 4.3.1 概念说明

不是每次调用都要调优。当用户不传 `config=` 时，`gemm_tuned` 会走一条「分析式默认」路径：blockscaled 用 `blockscaled_default_config`，普通纯 GEMM 先试 nvMMH 启发式、失败再回退 `default_config`。这一节讲三个东西：

1. **`default_config` / `_default_config_for_cap`**：按 `device_capacity` 给每代架构一个「开箱即用」的稳妥配置。
2. **`blockscaled_default_config`**：给 FP8/FP4 量化 GEMM 选默认 tile/cluster，大形状会特意选 `(256,256)` 以打开 `overlap_accum_sf`（第二级 TMEM 累加器舞台）。
3. **两个约束函数**：`blockscaled_config_ok` 判断一个 config 能否跑 blockscaled；`config_supports` 判断能否跑 gather_A / varlen_m；`cta_tile_shape_m` 计算「每 CTA 真正的 M tile」（SM100 的 2-CTA MMA 会把 tile_m 对半分）。

#### 4.3.2 核心流程

默认配置的选择链（在 `gemm_tuned` 里，见 [gemm_interface.py:L509-L532](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_interface.py#L509-L532)）：

```
config is None?
├─ blockscaled (有 SFA)?  → blockscaled_default_config(m, n, device_capacity)
├─ 纯 GEMM (无 varlen/gather/C/bias/quant)?  → 先试 nvmmh_config(A, B, cap)
└─ 仍为 None 或非纯 GEMM    → default_config(device)  → _default_config_for_cap(cap)
```

`_default_config_for_cap` 按架构返回的默认（这是 `device_capacity` 驱动选择的核心）：

| cap | 默认 tile | cluster | pingpong | dynamic_persistent |
|-----|-----------|---------|----------|--------------------|
| 8 (SM80) | 128×128, tile_k=32, 4 warps | (1,1) | False | False |
| 10/11 (SM100/110) | 256×256 | (2,1) | False | **True** |
| 12 (SM120) | 128×128 | (1,1) | **True** | True |
| 其它(默认=SM90) | 128×192 | (2,1) | **True** | False |

注意 SM100 与 SM90 的默认在「tile 大小、pingpong、动态调度」三个维度上都不同——这正反映了 Blackwell 与 Hopper 的架构差异。

**`cta_tile_shape_m` 的关键规则**：在 SM100/110 上，当 `cluster_m` 为偶数且 `tile_m ∈ {128,256}`（blockscaled 时只能是 `{256}`）时，硬件启用 **2-CTA MMA**，两个 CTA 合作算一个 tile，于是「每 CTA 的 M tile」折半：

\[\text{cta\_tile\_m} = \begin{cases} \text{tile\_m} / 2 & \text{SM100/110, cluster\_m 偶, tile\_m} \in \text{合法集} \\ \text{tile\_m} & \text{否则} \end{cases}\]

这个 per-CTA M 是 tile 调度器、OOB 边界、split-K partials 工作区、reduce-sink 插槽共同使用的计量单位——任何「按 M tile 分配的主机侧缓冲」都必须用它，否则尺寸会错。

**`blockscaled_config_ok` 的约束集**（SM100 路径）：`device_capacity ∈ {10,11}`、不能 `swap_ab`、`tile_k is None`（tile_k 由 MMA 指令推导）、`tile_m ∈ {128,256}`、`tile_n` 是 64 的倍数且在 \([64,256]\)、`cluster_m/n ≤ 4`（SF 多播每维最多 4 个 CTA）。SM120 路径约束更严：`tile_m/tile_n ∈ {128,256}`。这套约束是「**唯一**的真值来源」——调优剪枝和启发式候选空间都调用它。

#### 4.3.3 源码精读

`default_config` 入口只做「查设备能力 → 转发」：

[quack/gemm_config.py:L281-L285](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_config.py#L281-L285) —— `default_config(device)`：用 `get_device_capacity(device)[0]` 拿主版本号。

真正的分支表在 `_default_config_for_cap`（`@lru_cache` 缓存，因为每代架构只需算一次）：

[quack/gemm_config.py:L330-L372](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_config.py#L330-L372) —— `_default_config_for_cap(cap)`：四个分支对应 SM80/SM100/SM120/默认(SM90)。

blockscaled 默认配置会按形状分档，大形状选 `(256,256)` 以打开 `overlap_accum_sf`：

[quack/gemm_config.py:L288-L314](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_config.py#L288-L314) —— `blockscaled_default_config`：`m≥512 and n≥256` → `(256,256,(2,1))`；`m≥512 and n≥128` → `(256,128,(2,1))`；否则 `(128,128,(1,1))`。SM120 固定 `(128,128)` pingpong。

`cta_tile_shape_m`，注意它与 `GemmSm100.use_2cta_instrs` 互为镜像（docstring 要求「keep in sync」）：

[quack/gemm_config.py:L57-L68](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_config.py#L57-L68) —— `cta_tile_shape_m`：2-CTA MMA 时 tile_m 折半。

blockscaled 合法性约束（**唯一真值来源**）：

[quack/gemm_config.py:L71-L95](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_config.py#L71-L95) —— `blockscaled_config_ok`：SM100 与 SM120 两套约束，每条都带硬件原因注释。

这条约束被两处调用：①`gemm_interface.prune_invalid_gemm_configs`（[L414-L415](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_interface.py#L414-L415)）；②`gemm_runtime/autotune.py` 的 `_prune_for_mod`（[L189](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_runtime/autotune.py#L189)）。这种「一处定义、多处复用」避免了约束漂移。

#### 4.3.4 代码实践

**目标**：验证 `default_config` 的架构分支、`cta_tile_shape_m` 的折半规则、`blockscaled_config_ok` 的剪枝。

**步骤**（**无需 GPU**，直接用内部纯函数 `_default_config_for_cap`、`cta_tile_shape_m`、`blockscaled_config_ok`）：

```python
from quack.gemm_config import (
    GemmConfig, _default_config_for_cap, cta_tile_shape_m, blockscaled_config_ok,
    blockscaled_default_config,
)

# (1) 默认配置按 device_capacity 分流
for cap in (8, 9, 10, 12):
    d = _default_config_for_cap(cap)
    print(f"cap={cap}: tile=({d.tile_m},{d.tile_n}) cluster=({d.cluster_m},{d.cluster_n}) "
          f"pingpong={d.pingpong} dp={d.is_dynamic_persistent}")

# (2) cta_tile_shape_m：SM100 上 cluster_m 偶 + tile_m=256 → 折半为 128
print("256,(2,1),SM100,blockscaled:", cta_tile_shape_m(256, 2, 10, blockscaled=True))   # 128
print("256,(2,1),SM100,dense:     ", cta_tile_shape_m(256, 2, 10, blockscaled=False))  # 128
print("256,(1,1),SM100:           ", cta_tile_shape_m(256, 1, 10))                     # 256 (cluster_m 奇)
print("192,(2,1),SM90:            ", cta_tile_shape_m(192, 2, 9))                       # 192 (非 SM100)

# (3) blockscaled_config_ok：合法 vs 非法
ok   = GemmConfig(tile_m=128, tile_n=256, cluster_m=2, cluster_n=1, device_capacity=10)
bad1 = GemmConfig(tile_m=128, tile_n=224, cluster_m=2, cluster_n=1, device_capacity=10)  # tile_n 非 64 倍数
bad2 = GemmConfig(tile_m=128, tile_n=256, cluster_m=2, cluster_n=1, device_capacity=10, swap_ab=True)
print("ok:", blockscaled_config_ok(ok), "bad1:", blockscaled_config_ok(bad1),
      "bad2:", blockscaled_config_ok(bad2))

# (4) blockscaled 默认按形状分档
print("m=4096,n=4096,SM100:", blockscaled_default_config(4096, 4096, 10))  # 大形状 → (256,256,(2,1))
print("m=128,n=128,SM100 : ", blockscaled_default_config(128, 128, 10))    # 小形状 → (128,128,(1,1))
print("m=2048,n=2048,SM120:", blockscaled_default_config(2048, 2048, 12))  # SM120 固定 (128,128) pingpong
```

**预期**（按源码手算，**待本地验证**）：
- (1) 输出与上表四行一致（cap=10 → 256×256/(2,1)/dp=True；cap=9 → 128×192/(2,1)/pingpong=True）。
- (2) 折半结果依次为 `128 / 128 / 256 / 192`。
- (3) `ok: True bad1: False bad2: False`（tile_n=224 非 64 倍数；swap_ab 未测试）。
- (4) 大形状 `(256,256,(2,1))`、小形状 `(128,128,(1,1))`、SM120 `(128,128)` pingpong=True。

**需要观察的现象**：`cta_tile_shape_m` 只在 SM100/110 且 `cluster_m` 偶、tile_m 命中合法集时折半；`blockscaled_config_ok` 把 `tile_n=224` 这类「32 倍数但非 64 倍数」的配置卡掉（这正是 AGENTS.md 提到的「Blockscaled SF tmem 64-N 颗粒」约束）。

#### 4.3.5 小练习与答案

**Q1**：`_default_config_for_cap` 用了 `@lru_cache(maxsize=None)`，为什么安全且有益？
**答**：输入只有 `cap`（少数几个整数），输出是不可变的 `GemmConfig`；缓存后每代架构只构造一次，省去重复对象创建，且 `frozen` 保证返回值不会被调用方篡改。

**Q2**：为何 `blockscaled_config_ok` 在 SM100 要求 `tile_k is None`？
**答**：blockscaled 的 tile_k 不是自由选的，它由 MMA 指令（tcgen05 MMA 的 K 维 atom）和 SF atom 列推导（[L87](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_config.py#L87) 注释）。因此合法配置里 tile_k 必须留空交给内核推导，填了具体值反而不合法。

**Q3**：`cta_tile_shape_m` 与 `GemmSm100.use_2cta_instrs` 为什么要「keep in sync」？
**答**：两者描述同一硬件事实（是否启用 2-CTA MMA）。主机侧用它算缓冲尺寸 / tile 计数（如 split-K 的 partials 工作区，见 [gemm.py:L306-L307](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L306-L307)），设备侧用它决定 MMA tiler；若两者不一致，主机分配的缓冲与内核实际写入的尺寸会对不上。

---

## 5. 综合实践

把三个模块串起来：**假设你在 SM100 上做一次 mxfp8 blockscaled GEMM，形状 \(M=N=K=4096\)，不传 `config=`。请预测并验证全链路。**

1. **预测默认配置**：根据 4.3，`blockscaled_default_config(4096, 4096, 10)` 会选什么 tile/cluster？写出你的预测，再运行确认。
2. **验证 per-CTA M**：用 `cta_tile_shape_m` 算出这个默认配置下每个 CTA 实际负责的 M 行数，并解释为何折半。
3. **构造一个非法配置并解释**：手工构造 `GemmConfig(tile_m=128, tile_n=192, cluster_m=2, cluster_n=1, device_capacity=10)`，用 `blockscaled_config_ok` 验证它会被剪枝，并指出违反了哪条约束（提示：192 不是 64 的倍数）。
4. **对照调优入口**：打开 [gemm_interface.py:L465-L468](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_interface.py#L465-L468)，看清 `gemm_tuned` 的 `@autotune` 装饰器如何把 `get_all_configs()` 喂进候选池、又如何用 `prune_invalid_gemm_configs`（内部调 `blockscaled_config_ok`）剪枝。画出「全量候选 → 按 cap 过滤 → blockscaled 剪枝 → 测量选优」的数据流。

**预期**：默认 `(256,256,(2,1))`；per-CTA M 折半为 128；非法配置 `tile_n=192` 因「SF tmem 64-N 颗粒」被剔除。

## 6. 本讲小结

- `GemmConfig` 是一个 `frozen` 数据类，用 14 个标量字段（tile/cluster/pingpong/dynamic_persistent/swap_ab/split_k/device_capacity 等）完整描述一次 GEMM 的几何与策略；`SplitKMode` 是配套的 `IntEnum`，定义 SERIAL/PARALLEL/SEPARATE 三种 split-K 合并方式。
- `get_all_configs()` 用 `itertools.product` 为四代架构生成候选池，每个 config 自带 `device_capacity` 标签，运行时按真实 GPU 过滤——import 期不碰 CUDA。
- **SM100 没有 pingpong**：Blackwell 的 tcgen05 MMA 把累加器放进 TMEM 并配合 warp 分工异步流水线，MMA↔epilogue 重叠由硬件原生提供，故 `GemmSm100` 无 `pingpong` 形参，`_get_sm100_configs` 转而调优 `use_clc` 与 `use_tma_gather`。
- `default_config` 经 `_default_config_for_cap` 按 `device_capacity` 分流：SM80→128²/无 cluster，SM100→256²/动态调度，SM120→128²/pingpong，SM90→128×192/pingpong。
- `blockscaled_config_ok` 是 blockscaled 合法性的**唯一真值来源**，被调优剪枝与启发式候选两处复用；`cta_tile_shape_m` 镜像 SM100 的 2-CTA MMA 规则，凡按 M tile 分配的主机缓冲都必须用它。

## 7. 下一步学习建议

- 本讲只讲了「配置数据结构」。这些 config 如何被**编译 + 缓存 + 启动**，见 u4-l1（GEMM 编译与计划缓存，`_compile_gemm`/`_GemmPlan`/`run_gemm_plan`）。
- config 如何被**测量选优**，见 u8-l1（自动调优，`@autotune` 装饰器与 `prune_invalid_gemm_configs` 全貌）。
- config 的字段如何**真正驱动设备侧内核**（pingpong 在 SM90 主循环里长什么样、cluster 如何切 tile），见 u5（GemmBase / gemm_sm90 / gemm_sm100）。
- blockscaled config 字段（`SFA/SFB`、`bs_format_a/b`）的量化语义，见 u7-l1（Blockscaled 操作数与格式）。
