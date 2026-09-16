# u5-l3 梯度缓冲区与 reduce_grad

## 1. 本讲目标

学完本讲，你应该能够：

1. 画出训练侧的**梯度三缓冲**——`[E+B,H,H']` 梯度缓冲、预取槽梯度、`[R,B,H,H']` 归约缓冲——与权重布局的镜像关系，并解释预取槽梯度为什么必须藏在独立的归约缓冲里、绕开框架自身的梯度归约。
2. 读懂 `GradReduceKernel` 的两阶段执行：阶段一是 warp 特化的远程读 + 本地累加（含 prescan 预扫描），阶段二是跨 rank 屏障之后的**只本地清零**。
3. 理解「不远程清零」这一 NVLink 带宽权衡：读、写两个方向共享每 GPU 的单一 NVLink 预算。
4. 掌握 `launch_grad_reduce` 的契约校验、编译缓存与 `Buffer.reduce_grad` 生产路径，并理解 `tests/test_grad_reduce.py` 如何用「affine 精确 + randn 逐位」两级数据把累加顺序也钉死。

## 2. 前置知识

- **梯度归约（gradient reduce / all-reduce）**：数据并行训练中，每个 rank 对同一份参数算出各自的梯度后，必须跨 rank 求和才能得到完整梯度。训练框架（如 Megatron）自带这套逻辑，按参数的形状逐块归约。本讲的关键问题正是「哪些梯度行**不能**交给框架归约」。
- **动态冗余专家的梯度问题**（承接 u1-l1、u5-l1）：rank A 把 home 在 rank B 的专家 e 预取到本地槽位参与计算，反向传播就会在 rank A 上产生专家 e 的梯度。但参数 e 的「正本」梯度必须落在 rank B；框架的归约只认 rank A 自己名下的参数，所以这份「副本梯度」必须由 MoonEP 自己搬回家。
- **对称内存**（承接 u2-l2）：`create_nvl_dist_tensor` 把每个 rank 拥有的一段物理显存映射成全组一致的连续虚拟地址，任何 rank 都能经 NVLink 直接读/写其他 rank 的段。`[R,B,H,H']` 归约缓冲正是这样一块「每个 rank 贡献一段」的对称张量。
- **自复位跨 rank 屏障**（承接 u3-l6）：`cross_rank_barrier` 让 R 个 rank 的 cooperative grid 在内核里两两握手，不依赖宿主同步，且屏障状态每轮自复位。
- **fp32 加法不满足结合律的严格性**：浮点加法有舍入，`(a+b)+c ≠ a+(b+c)` 可能相差最后一个 ULP。因此「以什么顺序累加」本身就是内核契约的一部分，测试用 randn 数据逐位（bitwise）校验顺序。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [moonep/grad_reduce.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/grad_reduce.py) | 本讲主角：`GradReduceKernel` 设备内核 + `launch_grad_reduce` 宿主封装，全文件不到 540 行 |
| [moonep/api.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py) | `Buffer.reduce_grad` 生产入口与 `_launch_full_grad_reduces`（gate/up/down 三投影循环） |
| [README.md](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md) | 梯度缓冲布局的唯一官方文档（Gradient buffers 一节） |
| [tests/test_grad_reduce.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_grad_reduce.py) | 正确性测试：五种计划、两种数据、11 个用例 + 重复发射生命周期测试 |
| moonep/_common.py | `cross_rank_barrier`（u3-l6 已精读，本讲只引用） |
| moonep/planning.py | 复用其 `match_any_b32`（warp 内同值检测）与 `st_global_v4_s32`（16B 向量写）两个 PTX 助手 |

## 4. 核心概念与源码讲解

### 4.1 梯度三缓冲：与权重布局的 fp32 镜像

#### 4.1.1 概念说明

u5-l1 讲过权重侧的契约：每个投影（MoE FFN 的 gate/up/down 三个矩阵）持有一个连续的 `[E+B, H, H']` 对称内存张量，前 E 行是全组专家（物理上是各 home rank 的参数显存），后 B 行是本地预取槽。训练侧把这个布局**逐行镜像**成 fp32 梯度缓冲，共三份缓冲、三类角色：

1. **参数梯度行 `[0, E)`**：各 rank 自己名下 `epn = E/R` 个专家的参数梯度正本，最终交给框架做数据并行归约。
2. **预取槽梯度行 `[E, E+B)`**：本 rank 反向传播时为「预取来的远程专家」算出的梯度。**关键设计**：这些行的物理内存不是参数梯度缓冲，而是本 rank 在归约缓冲里那一段——即 `full_grad[E:E+B]` 与 `reduce_buffer[rank]` 是同一块内存的两个视图（alias）。
3. **归约缓冲 `[R, B, H, H']`**：一块对称内存，第 r 段物理上驻留在 rank r 的 GPU 上；经 VMM 映射后每个 rank 都能把它当成一个整体远程读写。

为什么预取槽梯度必须绕开框架的梯度归约？因为它是**临时的副本梯度**：专家 e 的完整梯度 = 「home rank 上本地算出的正本」+「所有预取了 e 的 rank 算出的副本」。如果副本梯度混进参数梯度缓冲，框架按参数形状做 all-reduce 时就会把这份副本错误地留在 rank A 名下（甚至重复计入），home rank 永远收不到。所以副本梯度被隔离进归约缓冲，由 MoonEP 自己的 `reduce_grad` 内核专门搬回 home rank——README 把这一点写成了显式约束：

> duplicated experts' grads are temporary and must stay invisible to the framework's own grad reduce.

#### 4.1.2 核心流程

一次 `reduce_grad`（所有 rank 同时执行同一个内核）的宏观流程：

```text
前提：各 rank 的反向传播已把预取槽梯度写进 full_grad[E:E+B]
      ≡ 归约缓冲中自己的段 reduce_buf[rank]

for 每个投影 (gate, up, down):                    # 共用同一份 plan
    每个 rank r 并行执行 GradReduceKernel:
        阶段 0  prescan: 扫描 experts_to_copy[R,B]，
                找出指向自己专家段 [r*epn, (r+1)*epn) 的所有槽
        阶段 1  按 128×128 tile 远程读这些槽（NVLink），
                以本地梯度为种子累加，写回自己段内的参数梯度行
        屏障    cross_rank_barrier：全组确认「读完了」
        阶段 2  每个 rank 只清零【自己段内】被消费过的槽
                （plan[rank,b] >= 0 的那些），供下个 microbatch 复用
```

用数学语言描述 rank \(r\) 上专家 \(e\) 的梯度更新（\(v_{s,b}\) 表示归约缓冲中 rank \(s\) 第 \(b\) 个槽的值）：

\[
g_e \;\leftarrow\; g_e \;+\; \sum_{\{(s,b)\,:\,\mathrm{plan}[s,b]=e\}} v_{s,b},\qquad e\in[r\cdot\mathrm{epn},\,(r+1)\cdot\mathrm{epn})
\]

串行加法顺序按展平序号 \(\mathrm{rb}=s\cdot B+b\) 升序——这不是实现细节的巧合，而是被 randn 测试逐位钉死的契约（见 4.4.3）。清零条件则是\[ \mathrm{plan}[r,b]\ge 0 \;\Rightarrow\; v_{r,b}\leftarrow 0 \]即「我段里被别人消费过的槽我负责清」，清零者是**段的拥有者**而非专家的拥有者——这正是「只写本地」的要点。

#### 4.1.3 源码精读

README 的 Gradient buffers 一节是布局的权威定义，三行 bullet 分别对应上面三个角色：

- [README.md:L61-L69](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L61-L69)：训练以 fp32 镜像权重布局；`[0,E)` 行是参数梯度；`[E,E+B)` 行由**独立的归约缓冲**背书而非参数梯度，物理内存来自进程级共享池（与预取池同款，额外开销与 MoE 层数无关）；归约缓冲把全部 R 个 rank 的段映射成一个 `[R,B,H,H']` 视图，`reduce_grad` 远程读 → 本地累加 → 清零自己消费过的槽。

生产路径上，三个投影共用一份 `experts_to_copy`，逐个调用内核；注意传给内核的是 `full_grad[:E]` 切片——内核只认参数梯度行，预取槽行由框架反向直接写入（经 alias 落进归约缓冲）：

- [moonep/api.py:L185-L219](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L185-L219)：`_launch_full_grad_reduces` 校验每个 `full_*_grad` 是连续 fp32 且第一维为 `E+B`，然后对 gate/up/down 三对 `(full_grad, reduce_buffer)` 各调一次 `launch_grad_reduce(full_grad[:E], reduce_buffer, experts_to_copy, ...)`。

- [moonep/api.py:L1085-L1120](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L1085-L1120)：`Buffer.reduce_grad` 的 docstring 把整条链路写成一句话——每个 rank 远程读取归约缓冲中属于自己专家的槽、累加进本地梯度行、清零自己消费过的槽；并强调它与 `combine` 分开成独立入口、跑在共享通信流上以串行化 Barrier/meta 资源。

- [README.md:L117-L128](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L117-L128)：API 演示——`reduce_grad` 在 dispatch bwd（即 `combine` 取回 token 侧梯度）之后调用，六个张量参数三三配对；注释明确 `full_*_grad` 的 `[E,E+B)` 行由归约缓冲背书。

另外，「训练必须 `B = E/R`」的约束在梯度侧同样成立（[README.md:L56-L59](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L56-L59)）：规划器保证每个 rank 至多从一个远程 home group 复制专家，因此归约缓冲的 B 个槽总能装下全部副本梯度。

#### 4.1.4 代码实践

**实践目标**：不用 GPU，用纯 PyTorch 在单进程里把「三缓冲镜像关系」跑通一遍，重点体会 `full_grad[E:E+B] ≡ reduce_buf[rank]` 的别名。

**操作步骤**：

1. 新建 `alias_demo.py`（示例代码，可放任意目录，仅需 CPU 版 PyTorch）：

```python
import torch

R, epn, H, Hp, B = 4, 4, 64, 32, 4
E = R * epn

# full_grad: [E+B, H, H']，模拟框架视角的完整梯度缓冲
full_grad = torch.zeros(E + B, H, Hp, dtype=torch.float32)
# reduce_buf: [R, B, H, H']，模拟对称内存归约缓冲
reduce_buf = torch.zeros(R, B, H, Hp, dtype=torch.float32)

# 用 data_ptr 验证生产布局中的别名关系：full_grad[E:] 与 reduce_buf[rank]
# 在真实系统里是同一块物理内存的两个视图。单进程模拟这一事实的办法是
# 直接让它们共享存储：
full_grad[E:] = reduce_buf[R - 1]        # 让第 R-1 个 rank 的槽行共享存储
print(full_grad[E:].data_ptr() == reduce_buf[R - 1].data_ptr())  # False：赋值是拷贝
# PyTorch 张量视图共享存储的等价写法（模拟 alias）：
alias = reduce_buf[R - 1]
alias.fill_(1.0)
print(full_grad[E:].abs().sum().item())  # 0 —— 除非真正 alias，否则互不影响
```

2. 运行 `python alias_demo.py`。

**需要观察的现象**：第一次打印为 `False`（普通切片赋值是值拷贝）；第二次求和为 `0`——这正说明**别名不是 PyTorch 默认行为，而是 MoonEP 显式构造的内存布局**：生产代码里 `full_grad` 的后 B 行是直接从归约缓冲切出来的视图，框架往 `full_grad[E:]` 写梯度时，物理上就是在写 `reduce_buf[rank]`。若想在本脚本里真正复现 alias，可用 `torch.empty` 先建 `[R,B,H,H']`，再令 `full_grad = torch.cat([param_grad, reduce_buf[rank]])` 之外的做法——实际上单张量无法跨段 alias，这正是 MoonEP 要用 VMM 才能实现的效果。

**预期结果**：两行输出 `False` 与 `0.0`。待本地验证（无需 GPU）。

#### 4.1.5 小练习与答案

**练习 1**：如果预取槽梯度不放在独立缓冲、而是直接放进 `[E+B,H,H']` 参数梯度缓冲的后 B 行，框架的数据并行梯度归约会出什么错？

**答案**：框架按参数缓冲的完整形状归约，会把后 B 行（副本梯度）当成 rank A 自己的参数梯度一起 all-reduce——home rank 永远收不到这份贡献，且副本梯度被错误地累进（或平均进）与预取槽对应位置无关的参数上，梯度直接错。正确语义要求副本梯度先经 `reduce_grad` 「路由」到 home rank 的正本行，再参与框架归约。

**练习 2**：为什么归约缓冲的物理内存要用「进程级共享池、跨层共享」（与预取池同款），而不是每个 MoE 层各建一块？

**答案**：VMM 映射与句柄交换的建链成本高（u2-l2/u2-l3），且跨 rank 对称内存要占用每 rank 一段的物理显存；所有层复用同一块 `[R,B,H,H']` 缓冲（配合每次用完即清零、下次 dispatch 重新规划），额外显存开销与 MoE 层数无关，只与单层最大 B 成正比。

**练习 3**：`reduce_grad` 之后、下一个 microbatch 的反向传播之前，归约缓冲处于什么状态？空槽（`plan == -1`）呢？

**答案**：被消费过的槽（`plan[rank,b] >= 0`）已被属主 rank 本地清零，可直接复写；空槽保持原值、无人触碰。注意下一轮 dispatch 会产出新的 plan，同一物理槽上一轮是否为空与下一轮无关——清零的判据永远是「本轮 plan 里该槽为活」。

### 4.2 GradReduceKernel（上）：prescan 预扫描与 warp 特化累加

#### 4.2.1 概念说明

`GradReduceKernel` 是一个**持久化（persistent）** 内核：grid 恒为 `num_sms` 个 CTA、cooperative 启动，所有工作以 grid-stride 循环分派。每个 CTA 内 5 个 warp 分工：

- **warp 0（load warp）**：用 2D TMA 把远程归约槽的 tile 搬进 smem 流水线；
- **warp 1–4（ACC warps，128 线）**：把本地梯度 tile 播种（seed）进 128 个 fp32 寄存器，逐 stage 累加远程 tile，最后向量写回。

进入流水线之前，内核先做一次 **prescan**：全局计划表 `experts_to_copy` 是 `[R,B]` 的 int32 矩阵（-1 表示空槽），每个 rank 只关心「指向**我**的专家段 `[rank*epn, (rank+1)*epn)` 的槽有哪些」。prescan 把它重组成三张 smem 表：

| 表 | 形状 | 语义 |
| --- | --- | --- |
| `off` | `[EPN+1]` | 按本地专家 le 的槽计数排他前缀和，`off[le+1]-off[le]` = 该专家的槽数 nslot |
| `alist` / `acnt` | `[EPN]` / `[1]` | 紧凑化的「活跃本地专家」表——没有任何远程槽的专家零成本跳过 |
| `slist` | `[R*B]` | 每个活跃专家的槽列表（rb 升序），元素是展平的槽号 `rb = src*B + b` |

另一张小表 `clist`/`ccnt` 记录**本 rank 自己段内**被消费过的槽号（`plan[rank,b] >= 0`），留给阶段 2 清零用。

为什么 prescan 必须是协作式的（全体线程 + 原子 + match_any），而不是单线程扫一遍？因为 `R*B` 个 int32 的串行 gmem 读取是一条**加载延迟链**：每次读几百 ns，R=8、B=32 时 256 次串行读就把内核开头卡死。协作式扫描把延迟摊到所有线程上。

#### 4.2.2 核心流程

阶段 1 的单个工作项（work item）定义在「活跃专家 × tile」网格上：

```text
total_work = acnt * (H/128) * (H'/128)        # 活跃专家数 × 每专家 tile 数
for work_idx in grid_stride(bidx, num_sms):
    le  = alist[work_idx // TILES_PER_EXPERT] # 哪个本地专家
    mt, nt = tile 坐标                          # 128×128 tile 的行/列块号
    nslot = off[le+1] - off[le]                # 该专家被多少个远程槽持有梯度

    warp 0:  for s in 0..nslot:
                 producer_acquire(stage)
                 TMA G2S: reduce_buf 的第 slist[beg+s] 行块 (mt, nt) → smem stage
    warp 1..4:
                 autovec_copy(本地 grad tile → acc_reg)     # 种子，16B 向量读
                 for s in 0..nslot:
                     consumer_wait(stage)
                     acc_reg += stage_flat[...]              # 128 线 fp32 累加
                     consumer_release(stage)                 # lane 0 发信号
                 autovec_copy(acc_reg → 本地 grad tile)      # 写回，16B 向量写
```

累加的算术语义即 4.1.2 的公式：种子是本地梯度，随后按 `slist` 的 rb 升序逐槽累加——`slist` 的顺序就是 randn 测试能逐位对拍的原因（见 4.2.3 的 match_any 填充）。

#### 4.2.3 源码精读

内核常量与 smem 预算检查：

- [moonep/grad_reduce.py:L46-L53](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/grad_reduce.py#L46-L53)：`M_BLOCK = N_BLOCK = 128`、`STAGES = 3`（load 流水线深度）、`ACC_THREADS = 128`（4 个 ACC warp）、`NUM_THREADS = 160`（5 warp）。
- [moonep/grad_reduce.py:L73-L93](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/grad_reduce.py#L73-L93)：构造期检查单个 128×128 fp32 tile 的三段流水 smem 不超过设备预算；`_smem_bytes` = tile 流水（`3×128×128×4 = 196608` B，128 对齐）+ mbarrier（48 B）+ prescan 八张 int32 表（`(3·epn + 4 + 2·R·B + B)·4` 向上取整 128）+ 256 B 杂项。合计约 197–200 KB——由此推断该内核要求设备 optin smem ≥ 约 198 KB（H100 级 227 KB 满足，A100 级 163 KB 会在构造期直接 `RuntimeError`）。

张量视图与 TMA 构造：`expert_grad` 按 `(E*H, Hp)` 的 2D 矩阵看（行 = 全局专家×H 展平），`reduce_buf` 按 `(R*B*H, Hp)` 看——**槽维度与行维度被压平成同一个行轴**，于是远程 tile 与本地 tile 共用同一个 2D TMA atom：

- [moonep/grad_reduce.py:L117-L138](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/grad_reduce.py#L117-L138)：`expert_grad` 布局 `(E*H, Hp)`、`reduce_buf` 布局 `(R*B*H, Hp)`，均为行主序、尾维 stride 1；`make_tiled_tma_atom` 以 `(M_BLOCK, N_BLOCK)` tiler 在 `reduce_buf` 上建 G2S 拷贝原子。
- [moonep/grad_reduce.py:L140-L148](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/grad_reduce.py#L140-L148)：launch 几何——grid 恒 `num_sms`、block 160 线、`cooperative=True`（跨 rank 屏障要求全 CTA 驻留）。

prescan 的三步协作：

- [moonep/grad_reduce.py:L216-L239](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/grad_reduce.py#L216-L239)：第一步，全体线程把 `experts[]` 减去 `rank_epn` 后 staging 进 smem（`sexp`，负值/越界即非本 rank 专家），再用 CTA 级 `atomic_add` 对 `off[e+1]` 计数做直方图；同一循环块里顺手收集 `clist`——条件 `sexp[rank*B+b] >= -rank_epn` 等价于 `experts[rank,b] >= 0`，即「我段里被消费过的槽」。
- [moonep/grad_reduce.py:L241-L255](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/grad_reduce.py#L241-L255)：第二步，单线程对 EPN 个桶做串行前缀和并顺手压缩出 `alist`（`off[le+1] > 0` 的专家）——这段是纯 smem 操作，没有加载延迟链问题。
- [moonep/grad_reduce.py:L256-L277](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/grad_reduce.py#L256-L277)：第三步，warp 0 按 32 个槽一块（`NCHUNK`）用 `match_any_b32`（来自 planning.py 的 PTX 助手）把每个槽散射进 `slist`：warp 内同专家的 lane 互相可见，用 `popc(peers & lanes_lt)` 算出块内相对位次、原子推进桶游标 `cur[e]`。**关键注释**：这样填充的最终顺序与串行扫描**逐位一致**（rb 升序）——正是 randn 测试逐位校验的前提。

流水线与 warp 分工：

- [moonep/grad_reduce.py:L279-L295](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/grad_reduce.py#L279-L295)：`PipelineTmaAsync` 以 `producer_group=1 线程`（warp 0 的 TMA 发射线程）、`consumer_group=4 warp`、`tx_count = 128×128×4` 字节创建；另建 smem tile 的 flat 视图 `stage_flat` 供 ACC warp 按线程连续段读取。
- [moonep/grad_reduce.py:L297-L308](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/grad_reduce.py#L297-L308)：`total_work = acnt[0] * TILES_PER_EXPERT`，grid-stride 分派；`work_idx` 除/模解开 `(专家, mt, nt)` 三元组，`beg/off` 给出该专家的槽区间。
- [moonep/grad_reduce.py:L310-L320](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/grad_reduce.py#L310-L320)：load warp 对 `nslot` 个槽逐个 `producer_acquire` 后发 TMA——源行块坐标是 `slist[beg+s] * MTILES + mt`，即「槽号 × 每槽行块数 + 行块偏移」，把 `[R*B*H, Hp]` 行轴的正确一段搬进 stage。
- [moonep/grad_reduce.py:L321-L366](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/grad_reduce.py#L321-L366)：ACC warp 的三段式。① 种子：`gview` 是本地梯度 tile 的每线程 `(32 组 × 4 连续元素)` 视图，**stride 必须是静态常量**且偏移 16B 对齐，`autovec_copy` 才能发射 128 位访问——注释警告逐元素赋值会把 128 寄存器累加器退化成栈 alloca 或标量 LDG/STG；且行偏移必须**先扩成 i64 再乘 Hp**（`expert_id * H * Hp` 在生产形状超过 2^31 元素，这是测试用例 `i64_offset_7168x3072` 钉住的点）。② 累加：逐 stage `consumer_wait`、128 线各加自己的 `H_PER = 16384/128 = 128` 个元素。③ 释放与写回：`consumer_release` 只由每 warp 的 lane 0 发信号（`consumer_group=4` 的信号线程约定），因此先 `fence_view_async_shared` + `sync_warp` 保证本 warp 32 线的读全部完成、再 release；跨 warp 的完成由 empty mbarrier 的 4 计数门控；最后 `autovec_copy` 16B 写回本地梯度。

顺带一提：`nslot` 可以超过 `STAGES=3`（fan_in 测试构造 nslot = 2R）——load warp 的 acquire 会自动在 stage 环上等待消费者释放，单个工作项内就发生流水线回绕。

#### 4.2.4 代码实践

**实践目标**：用 PyTorch 复现 prescan 的三张表（`off`/`alist`/`slist`）与工作项分解，验证「专家无槽零成本」与「slist rb 升序」两条性质。

**操作步骤**：

1. 新建 `prescan_sim.py`（示例代码，纯 CPU）：

```python
import torch

def prescan(plan, R, B, E):
    """返回 (off, alist, slist)，与内核 smem 三表语义一致。"""
    epn = E // R
    rank = 0                                  # 以 rank 0 为例
    flat = plan.reshape(-1).tolist()          # rb 升序展平
    local = [(rb, e - rank * epn) for rb, e in enumerate(flat)]
    buckets = [[] for _ in range(epn)]
    for rb, le in local:                      # 单线程串行扫描 = 内核的参考顺序
        if 0 <= le < epn:
            buckets[le].append(rb)
    off = [0]
    alist, slist = [], []
    for le in range(epn):
        off.append(off[-1] + len(buckets[le]))
        if buckets[le]:
            alist.append(le)
            slist.extend(buckets[le])         # 桶内天然 rb 升序
    return off, alist, slist

R, B, epn = 4, 8, 4
E = R * epn
torch.manual_seed(0)
live = torch.rand(R, B) < 0.6
ids = torch.randint(0, E, (R, B), dtype=torch.int32)
plan = torch.where(live, ids, torch.full_like(ids, -1))

off, alist, slist = prescan(plan, R, B, E)
print("off   =", off)
print("alist =", alist)          # 只有被远程槽指向的本地专家
print("slist =", slist)          # 展平槽号，全局升序
print("total_work =", len(alist) * (256 // 128) * (128 // 128))
assert slist == sorted(slist)    # rb 升序 —— randn 逐位对拍的根基
```

2. 运行 `python prescan_sim.py`。

**需要观察的现象**：`alist` 通常远短于 `epn`（稀疏）；`slist` 严格升序；`off` 相邻差即各专家 nslot。

**预期结果**：所有断言通过。对照 [moonep/grad_reduce.py:L297-L299](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/grad_reduce.py#L297-L299) 的 `total_work = acnt[0] * TILES_PER_EXPERT`——无槽专家一个 tile 都不做。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 ACC warp 读本地梯度用 `autovec_copy` 而不并入 TMA 流水线？

**答案**：本地读只需要 16B 向量加载、且其延迟恰好被「第一个远程 stage 的等待」隐藏（docstring L9-11 的原话：latency hides under the first remote-stage wait）；并入 TMA 反而多占一个 stage 的 smem（每 stage 64KB）并让 smem 预算超限。

**练习 2**：`consumer_release` 为什么每个 warp 只有 lane 0 发信号，还要先 `sync_warp`？

**答案**：`PipelineTmaAsync` 的 `consumer_group=4` 约定每 warp 由单一信号线程递减 empty mbarrier 计数；若 32 线都发信号会把计数减穿。但释放前必须保证**本 warp 全部 32 线的 smem 读都完成**，所以先 `fence_view_async_shared`（让异步 smem 读对 mbarrier 可见）再 `sync_warp`，lane 0 的信号才能代表全 warp；跨 warp 的完成由 mbarrier 计数到 0 统一表达。

**练习 3**：注释说「per-element fragment assignments … demote the 128-reg accumulator to a stack alloca」——为什么逐元素赋值会毁掉性能？

**答案**：128 个 fp32 累加元素本应驻留寄存器、循环间零搬运；一旦编译器无法证明访问是静态步长的向量视图，就只能把 fragment 落到栈上（local memory，物理在显存）或退化为逐元素标量 LDG/STG，每个 stage 循环多出成百上千次访存。静态 stride 的 `(H_PER//4, 4)` 双层视图 + `assume(divby=4)` 就是为了保住寄存器驻留与 128 位访问。

### 4.3 GradReduceKernel（下）：跨 rank 屏障与「只本地清零」的带宽权衡

#### 4.3.1 概念说明

阶段 1 里，rank r 远程读的是**别的 rank 段里**的槽；阶段 2 要清零的是**自己段里**被别人消费过的槽。两者之间必须有一道全组屏障：如果 rank r 在读完之前、别的 rank 就清掉了 r 还没读的槽，梯度就会丢贡献。这道屏障用 u3-l6 精读过的 `cross_rank_barrier` 在内核内部完成——cooperative grid 栅障 + 跨 rank 原子握手 + 两道 proxy fence，不回落宿主。

清零的方式是这个内核最有趣的工程决策：**每个 rank 只写自己的本地显存**（清 `reduce_buf[rank]` 段），而**不是**让消费它的人远程写零。理由是 NVLink 的带宽经济学：每 GPU 的 NVLink 总带宽是**读写共享**的单一预算——如果读、写两个方向同时跑，各方向大约只能拿到一半。阶段 1 是纯远程读（希望吃满读方向）；若阶段 2 的清零也走远程写，它会与阶段 1 的读抢同一根管子，把阶段 1 拉长的时间远超本地清零的成本。而本地清零只消耗 SM↔L2 的写吞吐，与 NVLink 完全正交，还能与阶段 1 的时间重叠掉（屏障后各 rank 独立进行）。

#### 4.3.2 核心流程

```text
阶段 1 结束（本 rank 已读完所有需要的远程槽）
    │
cross_rank_barrier(meta, meta_stride, barrier_off, rank, R, ...)
    │   全组 R 个 cooperative grid 互相确认：
    │   「我段里的槽，所有需要它的人都读完了」
    ▼
阶段 2：for k in 0..ccnt:                    # 本 rank 消费过的槽（clist[k]）
            slot_base = (rank*B + clist[k]) * H * Hp     # i64！
            for v in grid_stride(..., VECS):             # VECS = H*Hp/4
                st_global_v4_s32(reduce_buf + slot_base + 4v, 0,0,0,0)
                                                    # 16B 向量写零
```

清零量按槽计：一个槽是连续的 \(H\times H'\) 个 fp32，即 \(4\cdot H\cdot H'/16 = H\cdot H'/4\) 个 16B 向量。以 `H=7168, H'=3072` 计，单槽约 88 MB、约 5.5M 个向量 store——所以必须用 grid-stride 把全体 CTA×线程撒上去，且注释明确：这活儿的上限是 SM↔L2 写吞吐，16B 向量 store 已经打满，标量 4B 不够、更重的 TMA bulk S2G 零填充也不会更快。

#### 4.3.3 源码精读

模块 docstring 的最后一段把这个权衡写得非常清楚，值得整段读：

- [moonep/grad_reduce.py:L20-L26](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/grad_reduce.py#L20-L26)：累加永远不写归约缓冲；所有 tile 求和完成后先跨 rank 屏障给所有对端上 fence，然后每个 rank 只本地清零自己消费过的槽。**远程写清零（标量或 bulk S2G）试过并被否决**：并发的远程读 + 远程写共享每 GPU 的单一 NVLink 预算（每方向约一半），搭「空闲」写方向的便车反而把阶段 1 拉得远比本地清零的成本长。

屏障调用与清零循环：

- [moonep/grad_reduce.py:L368-L372](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/grad_reduce.py#L368-L372)：阶段 1 的 grid-stride 循环结束后，全体线程进入 `cross_rank_barrier`（从 `_common.py` 导入，见 [moonep/_common.py:L275](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L275)），参数带上 meta 缓冲、屏障偏移、rank、R 与 cooperative 栅障计数器。
- [moonep/grad_reduce.py:L373-L389](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/grad_reduce.py#L373-L389)：清零循环本体。`cons = ccnt[0]` 是本 rank 被消费的槽数；槽基址 `(rank*B + clist[k]) * H * Hp` 用 **i64** 计算（同样是 2^31 溢出防护）；内层 `grid_stride` 以 `num_sms * NUM_THREADS` 为步长遍历 `VECS = H*Hp/4` 个 16B 向量，用 planning.py 导入的 `st_global_v4_s32`（一条 `st.global.v4.s32` PTX）写四个 int32 零——位模式即 fp32 的 0.0f。注释同时说明：这受 SM↔L2 写吞吐限制，向量 store 已饱和，标量 4B 差、TMA bulk S2G 零填充无益。

屏障的自复位性直接支撑生产用法「每个训练步一次 launch」：测试 `test_grad_reduce_repeated_launch`（[tests/test_grad_reduce.py:L441-L447](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_grad_reduce.py#L441-L447)）在同一 Buffer 上连发三轮（含一轮全空计划），验证屏障槽与 cooperative grid 计数器在两次 launch 之间自复位、空轮不会把屏障状态卡死或错位。

#### 4.3.4 代码实践

**实践目标**：定量体会「远程清零 vs 本地清零」的带宽账。

**操作步骤**：

1. 新建 `bandwidth_account.py`（示例代码，纯计算，无 GPU 依赖）：

```python
R, B, H, Hp = 8, 32, 7168, 3072
epn = 32                       # E = 256
slot_bytes = H * Hp * 4
link_GBps = 450                # 每 GPU NVLink 总带宽，量级参考（不同代数不同）

# 假设极端情况：所有 rank 的所有活槽都指向别的 rank（fan-in 拉满）
# 阶段 1 每 rank 需要远程读的字节（乐观：槽数 = R*B 中活槽比例 0.6）
live_ratio = 0.6
remote_read_bytes = R * B * live_ratio * slot_bytes / R   # 每个槽被一个 owner 读

# 方案 A：阶段 2 本地清零 —— NVLink 只承担读
t_read_A = remote_read_bytes / link_GBps

# 方案 B：阶段 2 远程清零 —— 清零写与阶段 1 的读共享预算（每方向约半）
clear_bytes = B * live_ratio * slot_bytes          # 每 rank 清自己段
t_read_B  = remote_read_bytes / (link_GBps * 0.5)  # 读方向被写方向挤掉一半
t_write_B = clear_bytes / (link_GBps * 0.5)

print(f"slot = {slot_bytes/2**20:.1f} MiB")
print(f"A(本地清零): NVLink 时间 ≈ {t_read_A*1e3:.2f} ms（清零与本地带宽正交）")
print(f"B(远程清零): 读 ≈ {t_read_B*1e3:.2f} ms + 写 ≈ {t_write_B*1e3:.2f} ms")
print(f"B 把阶段 1 拉长 ≈ {(t_read_B/t_read_A - 1)*100:.0f}%")
```

2. 运行并调整 `live_ratio`、`link_GBps` 观察结论是否翻转。

**需要观察的现象**：方案 B 里读方向被砍半导致阶段 1 接近翻倍，而省下的「本地清零」时间本来就不在 NVLink 关键路径上——结论稳定不翻转，这正是源码注释「riding the idle write direction stretches phase 1 far more」的定量版本。

**预期结果**：B 的读时间约为 A 的 2 倍。待本地验证（数字是量级示意，`link_GBps` 请按实际硬件代数替换）。

#### 4.3.5 小练习与答案

**练习 1**：清零条件是 `plan[rank,b] >= 0`，即「我段里**被别人消费**的槽」。如果某槽指向的是我自己段内的专家（自发自收，`plan[r,b] ∈ [r*epn,(r+1)*epn)`），会发生什么？

**答案**：完全正常：阶段 1 我自己把它累加进本地梯度（TMA 读的是自己段，走的是同一 `slist` 路径，测试 fan_in 用例专门覆盖 self-sends），阶段 2 我清它。自发自收不改变「段的拥有者清零」规则，只意味着读写两端物理上都在本地。

**练习 2**：为什么清零循环用裸 PTX `st_global_v4_s32` 而不是 `tensor.zero_()` 或 TMA？

**答案**：这是 CuTe DSL 设备代码，没有宿主张量方法可用；且注释已实测：清零瓶颈是 SM↔L2 写吞吐，16B 向量 store 恰好打满，标量 store 不够、TMA bulk S2G（要占 mbarrier/bulk_group 机制）也不会更快，最简单的指令就是最优的。

**练习 3**：如果去掉阶段 2 前的 `cross_rank_barrier`，最快的出错方式是什么？

**答案**：rank r 的 load warp 还在 TMA 读 rank s 的槽时，rank s 已跑完自己的阶段 1、率先清零该槽——r 把部分清零后的值（甚至全零）累进梯度，造成**静默的数值丢失**（不越界、不崩溃），这正是测试要靠逐位比对才能抓出的那类错误。

### 4.4 launch_grad_reduce 封装、生产路径与测试语义

#### 4.4.1 概念说明

`launch_grad_reduce` 是标准的「宿主封装三件套」：契约校验 → lru_cache 编译查询 → 构指针发射。它与 `Buffer.reduce_grad` 的分工是：前者管单个投影的一次内核发射（可被测试独立调用），后者管用户面（三投影循环、comm stream 异步、plan 有效性）。测试文件则把内核语义拆成**五维覆盖**：形状（含 i64 溢出）、计划拓扑、数据舍入特性、生命周期（重复发射）、边界（空计划、单 CTA）。

#### 4.4.2 核心流程

```text
Buffer.reduce_grad(plan, ..., 6 个梯度张量)
    ├─ 断言 plan 是 MoonEPCommPlan、六张量齐备
    ├─ 同步模式: 当前流直接跑 _launch_full_grad_reduces
    └─ 异步模式: 主流 record_event → comm 流 wait_event
                 → comm 流上跑三个投影 → 返回 event

_launch_full_grad_reduces: 对 gate/up/down 各调
    launch_grad_reduce(full_grad[:E], reduce_buffer, plan.experts_to_copy, ...)

launch_grad_reduce:
    空 tensor 提前返回 → 9 组契约断言
    → _get_compiled(E,H,Hp,R,B,meta_stride,num_sms,dev)  # lru_cache
    → make_ptr × 5 + 当前流 → compiled(...)
```

#### 4.4.3 源码精读

- [moonep/grad_reduce.py:L437-L470](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/grad_reduce.py#L437-L470)：`launch_grad_reduce` 的完整签名与 docstring。两个要点：① 当前 rank 只更新 owner 范围 `rank*(E//R) : (rank+1)*(E//R)` 内的梯度行；② **调用方必须保证所有 rank 都已写完 `remote_reduce_buffers` 才能在任一 rank 上发射**——生产路径靠共享 comm 流的串行化满足（[moonep/api.py:L1118-L1119](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L1118-L1119)），测试路径靠 `torch.cuda.synchronize()` + `dist.barrier`（[tests/test_grad_reduce.py:L401-L402](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_grad_reduce.py#L401-L402)）。
- [moonep/grad_reduce.py:L471-L507](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/grad_reduce.py#L471-L507)：契约校验——空张量提前返回；三个张量的 dtype/连续性/维度（`[E,H,H']` fp32、`[R,B,H,H']` fp32、`[R,B]` int32）；`experts_to_copy` 形状必须与 reduce buffer 的 `(R,B)` 严格一致、buffer 的 H/H' 与梯度一致；`E % R == 0`；`H % 128 == 0` 且 `H' % 128 == 0`（tile 整除）；num_sms 正整数；三个张量同设备。
- [moonep/grad_reduce.py:L392-L434](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/grad_reduce.py#L392-L434)：`_get_compiled` 以 `(E,H,Hp,R,B,meta_stride,num_sms,device_index)` 为键做 `lru_cache`——形状烧进 cubin、运行期只换指针；smem 预算取 `shared_memory_per_block_optin - 1024`（也缓存）。
- [moonep/grad_reduce.py:L509-L539](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/grad_reduce.py#L509-L539)：构 5 个 `make_ptr`（两个 fp32 指针 assumed_align=16，experts 表 assumed_align=4）+ 当前流句柄，调用编译产物。

测试侧的三个精读点：

- **计划拓扑**（[tests/test_grad_reduce.py:L40-L110](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_grad_reduce.py#L40-L110)）：五种 builder 都是 `(R,B,epn,seed)` 的确定性函数（绝不依赖本地 rank，否则各 rank 构出的全局表不一致）。`ring` 是默认环；`fan_in` 让每个 dst rank 的一个专家收 `2R` 个槽——超过 `STAGES=3`，逼出单工作项内的流水线回绕，并覆盖自发自收与同源重复专家；`empty` 全 -1（`total_work=0`、零清零，内核必须空着过两道屏障）；`asymmetric` 让 rank 0 只发不收、rank 1 只收不发，验证屏障在极端不均衡工作量下的步调一致；`random` 按 60% 活率随机。
- **数据与期望**（[tests/test_grad_reduce.py:L122-L172](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_grad_reduce.py#L122-L172)）：`affine` 模式（`expert*17 + row*0.125 + col*0.0078125` 等）保证所有加法精确（值都落在 24 位尾数内），期望与累加顺序无关；`randn` 模式有舍入，`_expected_for_rank` 按 src 升序、b 升序（即 rb 升序）串行累加——与内核顺序**逐位**一致，把「slist 必须 rb 升序」变成硬约束。slot 值要求在同种子的每 rank 上逐位可复现（依赖同构节点上 Philox 生成器的一致性）。
- **验证四断言**（[tests/test_grad_reduce.py:L181-L225](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_grad_reduce.py#L181-L225)）：`ok_grads`（本地段逐位等于期望）、`ok_nonlocal`（非本地行必须保持原值——内核绝不越权写别人的专家）、`zero_ok`（所有活槽清零）、`unchanged_ok`（空槽原封不动）；任一 rank 失败经 `all_reduce(MIN)` 全体报错。文件头注释（[tests/test_grad_reduce.py:L6-L19](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_grad_reduce.py#L6-L19)）把这五维覆盖轴写成了一页纸的测试设计文档，值得整读。

#### 4.4.4 代码实践

**实践目标**：按规格要求，用 PyTorch 写 `reduce_grad` 的**多 rank 单进程模拟**，在 `tests/test_grad_reduce.py` 的 `grad_base`/`reduce_base`（即 `_grad_values`/`_slot_values`）构造下验证期望结果——这是本讲的综合实践前置版本，也是理解内核语义最直接的方式。

**操作步骤**：

1. 新建 `grad_reduce_sim.py`（示例代码，纯 CPU 即可运行）：

```python
import torch

# ---- 与 tests/test_grad_reduce.py 逐位一致的数据构造 ----
def grad_values(E, H, Hp):                     # _grad_values：参数梯度底值
    expert = torch.arange(E, dtype=torch.float32).view(E, 1, 1)
    row    = torch.arange(H, dtype=torch.float32).view(1, H, 1)
    col    = torch.arange(Hp, dtype=torch.float32).view(1, 1, Hp)
    return expert * 17.0 + row * 0.125 + col * 0.0078125

def slot_values(rank, B, H, Hp):               # _slot_values：各 rank 槽底值
    slot = torch.arange(B, dtype=torch.float32).view(B, 1, 1)
    row  = torch.arange(H, dtype=torch.float32).view(1, H, 1)
    col  = torch.arange(Hp, dtype=torch.float32).view(1, 1, Hp)
    return (rank + 1) * 101.0 + slot * 11.0 + row * 0.03125 + col * 0.00390625

def plan_ring(R, B, epn):                      # _plan_ring 的忠实拷贝
    plan = torch.full((R, B), -1, dtype=torch.int32)
    for src in range(R):
        dst = (src + 1) % R;            plan[src, 0] = dst * epn + (src % epn)
        if B > 1:
            dst = (src + 2) % R;        plan[src, 1] = dst * epn + ((src + 1) % epn)
        if B > 2:
            dst = (src + R - 1) % R;    plan[src, 2] = dst * epn + ((src + 2) % epn)
    return plan

# ---- 内核语义的 PyTorch 模拟 ----
def reduce_grad_sim(grads, reduce_buf, plan):
    """grads: R 个 [E,H,H']（原址累加）; reduce_buf: [R,B,H,H']; plan: [R,B] int32"""
    R, B = plan.shape
    E = grads[0].shape[0]
    epn = E // R
    for r in range(R):                        # 每个 rank 只写自己的 owner 段
        for src in range(R):                  # rb 升序 = (src, b) 双层升序
            for b in range(B):
                e = int(plan[src, b])
                if r * epn <= e < (r + 1) * epn:
                    grads[r][e] += reduce_buf[src, b]   # 远程读 + 累加
    # cross_rank_barrier 之后：每 rank 只清零自己段内被消费的槽
    for r in range(R):
        for b in range(B):
            if int(plan[r, b]) >= 0:
                reduce_buf[r, b].zero_()

if __name__ == "__main__":
    R, epn, H, Hp, B = 4, 4, 128, 128, 4      # 对应 CASES 里的 single_tile
    E = R * epn
    plan = plan_ring(R, B, epn)
    grads = [grad_values(E, H, Hp) for _ in range(R)]
    reduce_buf = torch.zeros(R, B, H, Hp, dtype=torch.float32)
    for r in range(R):                        # 各 rank 的 backward 写自己的段
        reduce_buf[r] = slot_values(r, B, H, Hp)

    reduce_grad_sim(grads, reduce_buf, plan)

    for r in range(R):                        # 四断言，对齐 _verify 的口径
        expected = grad_values(E, H, Hp)      # 独立重算期望（_expected_for_rank）
        for src in range(R):
            vals = slot_values(src, B, H, Hp)
            for b in range(B):
                e = int(plan[src, b])
                if e >= 0:
                    expected[e] += vals[b]
        assert torch.equal(grads[r][r*epn:(r+1)*epn],
                           expected[r*epn:(r+1)*epn]), f"rank {r} grads"
        keep = torch.ones(E, dtype=torch.bool); keep[r*epn:(r+1)*epn] = False
        assert torch.equal(grads[r][keep], grad_values(E, H, Hp)[keep]), "nonlocal"
    for r in range(R):
        for b in range(B):
            if int(plan[r, b]) >= 0:
                assert torch.equal(reduce_buf[r, b], torch.zeros(H, Hp)), "zero"
            else:
                assert torch.equal(reduce_buf[r, b],
                                   slot_values(r, B, H, Hp)[b]), "unchanged"
    print("grad_reduce_sim: all checks passed")
```

2. 运行 `python grad_reduce_sim.py`。
3. 把 `plan_ring` 换成 `fan_in`/`empty`/`asymmetric` 版本（照抄测试 L59-L92），确认同一模拟全部通过。

**需要观察的现象**：四组断言全绿；换成 `fan_in` 后单专家 nslot 变 2R 但结果不变；换成 `empty` 后梯度原封不动、归约缓冲也原封不动（没有活槽要清）。

**预期结果**：`all checks passed`。由于 affine 底值的加法全部精确，模拟与真实内核在任何累加顺序下都应逐位一致。待本地验证（无需 GPU）。有 8 卡 NVLink 环境时，再跑 `torchrun --nproc_per_node=8 -m pytest -s tests/test_grad_reduce.py` 对照真实内核的 11 个用例。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `launch_grad_reduce` 收到的是 `full_grad[:E]` 切片，而内核里还要再限制「只更新 owner 段」？

**答案**：`[:E]` 切掉预取槽行是因为内核不应触碰它们（它们由框架反向写入、由归约缓冲背书）；而 `[E]` 范围内仍有 R 个 rank 的 owner 段——多 rank 共享同一份 `expert_grad` 对称视图时，若不限制到自己的 `rank*epn:(rank+1)*epn` 段，远程 rank 的写会互相踩踏（测试的 `ok_nonlocal` 断言专门防这个）。

**练习 2**：`randn` 测试为什么必须依赖「torch Philox 生成器同种子在各 rank 产生相同位」这一前提？它同构节点假设不成立时会怎样？

**答案**：参考实现 `_expected_for_rank` 在每个 rank 本地**重建所有远程 rank 的槽值**再求期望；若同一种子在异构节点（不同 GPU 架构/驱动）生成不同位，参考值本身就错了，测试会假阴性/假阳性。文件头注释明确标注该前提「holds on homogeneous nodes」。

**练习 3**：`Buffer.reduce_grad` 为什么不像 `dispatch`/`combine` 那样返回张量，而是返回 `None` 或 event？

**答案**：它是**原址累加**语义——结果直接落在调用方传入的 `full_*_grad` 前 E 行里，没有新张量产生；异步模式只需返回 event 让调用方知道何时可读。这也意味着调用方要自己保证「反向传播写完梯度、reduce_grad 完成前不得启动框架归约」的流上顺序。

## 5. 综合实践

把 4.4.4 的模拟升级为一个**带状态复用**的两轮训练步模拟（示例代码，纯 CPU）：

1. 构造 `R=4, epn=4, H=256, Hp=128, B=5`（对应测试 `multi_tile`），第一轮用 `ring` 计划、第二轮用 `random(seed=1)` 计划——模拟真实训练中「每个 microbatch 重新 dispatch 规划、reduce_grad 清零后缓冲复用」。
2. 每轮：重写各 rank 槽底值（可换 randn 种子）→ 跑 `reduce_grad_sim` → 校验四断言。
3. 关键检查点：**第一轮结束时的清零是否足以让第二轮正确**——如果第一轮漏清某个槽（试着注释掉清零循环里的一行观察），第二轮该槽的旧值会被错误累进新期望吗？为什么测试 `repeated_launch` 把中间一轮特意设成空计划？
4. （可选，需 8×GPU + NVLink）在同一环境下跑 `torchrun --nproc_per_node=8 -m pytest -s tests/test_grad_reduce.py::test_grad_reduce_repeated_launch -q`，对照真实内核的自复位屏障行为。

预期：两轮全部通过；漏清会在下一轮表现为「某个空槽/旧槽贡献被累入」——具体是否可见取决于新一轮 plan 是否复用该槽，这正是清零判据绑定「本轮 plan」而非历史的原因。待本地验证。

## 6. 本讲小结

- 训练侧梯度布局是权重布局的 fp32 镜像：`[E+B,H,H']` 梯度缓冲中 `[0,E)` 是参数梯度正本，`[E,E+B)` 由独立的 `[R,B,H,H']` 对称归约缓冲背书（`full_grad[E:] ≡ reduce_buf[rank]`），使副本梯度对框架自身的归约不可见。
- `reduce_grad` 的语义三步：prescan 找「指向我专家段」的槽 → 远程读 + 以本地梯度为种子的 fp32 累加（slist 严格 rb 升序，被 randn 测试逐位钉死）→ 屏障后每个 rank 只本地清零自己段内的活槽。
- `GradReduceKernel` 是 5-warp 特化持久化内核：warp 0 的 2D TMA G2S 流水 + warp 1–4 的 128 线寄存器累加；prescan 用 CTA 原子 + `match_any` 协作完成以保证与串行扫描逐位同序；本地 tile 用静态 stride 的 16B `autovec_copy` 播种/写回，地址偏移全程 i64（生产形状 `expert_id*H*Hp` 超 2^31）。
- 清零走本地而非远程是 NVLink 带宽经济学：每 GPU 读写共享单一预算，远程清零会把纯读的阶段 1 砍掉约一半带宽，代价远超本地清零（本地清零只占 SM↔L2 写吞吐，16B 向量 store 已饱和）。
- `launch_grad_reduce` 是「校验 → lru_cache 编译 → 发射」三件套，契约含 fp32/int32 dtype、连续性、`E%R`、`H,H'%128`；生产入口 `Buffer.reduce_grad` 对 gate/up/down 三投影共用同一 plan，可选 comm stream 异步。
- 测试五维覆盖（形状/拓扑/数据/生命周期/边界）中，affine 数据验证顺序无关的正确性、randn 数据把累加顺序变成逐位契约，`i64_offset_7168x3072`、`fan_in`、`empty`、`asymmetric`、`repeated_launch` 各钉住一个具体风险点。

## 7. 下一步学习建议

- 下一讲 u6-l1「异步通信流与 CUDA 事件」将展开本讲已经两次遇到的 `_record_streams`/`record_event`/`wait_event` 握手，弄清 `async_finish=True` 下梯度张量的生命周期保护。
- u6-l3「训练全链路：fwd/bwd 四象限」把本讲的 `reduce_grad` 放回完整的训练步象限图（dispatch fwd → prefetch → FFN → combine fwd → dispatch bwd → combine bwd → reduce_grad），建议先自己画一遍调用顺序再读。
- 想继续深挖源码的读者：对照读 [moonep/_common.py:L275](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L275) 的 `cross_rank_barrier` 实现（u3-l6 已精读其双相位自复位协议），以及 [benchmarks/bench_grad_reduce.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_grad_reduce.py) 如何单独度量这个内核的带宽（u6-l5 会讲基准口径）。
