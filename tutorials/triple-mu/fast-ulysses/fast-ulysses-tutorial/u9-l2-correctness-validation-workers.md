# u9-l2 correctness 与 validation 工作负载

## 1. 本讲目标

学完本讲，你应该能够：

1. 逐项列出 `correctness.py` 覆盖的验证维度（双模式、偶均分 sweep、不均匀分片、`out=` 三态、async、往返、20 轮同窗口、dtype 全表、backward、forward AD、double backward、off-stream、meta、timed），并说出每一维用什么作判据。
2. 解释「位精确对照」为什么选 `all_to_all_single` + `permute`（以及不均分时的 `dist.all_to_all` 列表版）作参照，而不是用本库自己的 plan 反推结果。
3. 写出「backward 的 splits 原样传递」的数学依据：mode 1 是 mode 0 的逆映射，两张 splits 表描述的是组的所有权几何而非单次收发量。
4. 说明 `validation.py` 如何用「消息片段 + 每 rank 一致 + 逐条 barrier」把 docs/api.md 的 Raises 全表变成可执行契约，以及「验证必须先于第一个握手」为什么靠 worker 的结构（而非单条断言）来检验。

本讲是 u9-l1 测试架构的续篇：u9-l1 讲了四层测试怎么组织、怎么跑，本讲钻进其中两个最重的多卡 worker，看它们各自「证明什么、证明不了什么」。

## 2. 前置知识

- **位精确（bit-exact）**：两个张量逐元素相等（`torch.equal`），不引入任何容差。fast-ulysses 的算子是纯数据搬运——不缩放、不归约、不改数值——所以「差不多」在任何地方都不算通过；一个错误的寻址会搬错字节，而不是让数值略微偏差。
- **参照（reference）**：一条与被测实现**不共享代码**的独立路径。correctness.py 的参照走 `torch.distributed`（NCCL），被测实现走本库的拷贝引擎路径，两者只在「语义」上应当一致。
- **Raises 契约**：docs/api.md 的 [Raises 一节](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md#L93) 承诺了一批「什么输入会 raise、消息说什么」。validation.py 把每一条变成断言：必须 raise、消息必须含指定片段。
- **先于第一个握手**：一次调用是「校验 → 开路屏障 → 传输 → 收尾屏障 → copy_out」（u6-l2）。若某个拒绝只在部分 rank 上发生、或发生在开路屏障之后，其余 rank 会卡在 GPU 自旋屏障里——表现为**挂起**而非测试失败。所以「拒绝必须全 rank 一致且先于屏障」是可运行性要求，不只是风格要求。
- **splits 的所有权语义**：`seq_splits[p]` / `head_splits[p]` 是「rank p 拥有第 p 段序列、第 p 段头」这一**组的几何属性**（u2-l3、u3-l1），与单次调用谁发给谁、发多少无关。这是本讲 4.3 的核心。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [test/distributed/correctness.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L1-L455) | 多卡正确性 worker：对 torch.distributed 路径做位精确对照，覆盖全部调用形态；附 epoch/staging 两个探针 |
| [test/distributed/validation.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/validation.py#L1-L395) | 多卡负向 worker：断言每条文档化拒绝都 raise、消息匹配、全 rank 一致且不挂起 |
| [test/test_distributed.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_distributed.py#L50-L63) | torchrun 包装器：把两个 worker 作为子进程按多种世界规模参数化运行（u9-l1 精读） |
| [test/test_plan.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L1-L193) | 无 GPU 布局测试：numpy 重放 CopyOp + all_to_all_single 参照；一字节 dtype 的绝对参照在此参数化 |
| [src/a2a_plan.cc](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L143-L216) | `build_plan`：mode 0 / mode 1 两分支的拷贝发射——4.3 数学推导的落点 |
| [src/group.cc](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L45-L136) | `make_dims`：dtype 白名单、16 字节对齐、mode 检查、splits 交叉校验——validation.py 大部分消息的产地 |
| [src/bindings.cc](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L26-L174) | `prepare()` 校验链与 vmap 拒绝内核；A2AFunction 的 backward（L500-L559） |
| [python/fast_ulysses/group.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L125-L197) | Python 侧前置拒绝：`out=`/async 的 autograd 拒绝、`out=`+`lend` 互斥 |

运行方式（继承 u9-l1）：`pytest test/test_distributed.py` 会按可见卡数参数化跑这两个 worker；调试时直接 `torchrun --nproc_per_node=8 test/distributed/correctness.py`。

## 4. 核心概念与源码讲解

### 4.1 位精确参照：correctness.py 的主轴与维度清单

#### 4.1.1 概念说明

correctness.py 回答一个问题：**在每一条调用路径上，结果是否与 torch.distributed 路径位精确一致？**它的开篇就把标准立死：

> Pure data movement, so "close enough" is not a passing result anywhere in this file.
> （[test/distributed/correctness.py:L5-L9](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L5-L9)）

为什么参照选 `all_to_all_single` + `permute`？三个理由：

1. **独立性**：参照完全走 `torch.distributed`（NCCL 集合通信），与本库的 plan 生成、拷贝引擎传输**零共享代码**。若用本库自己的 `build_plan` 反推期望结果，寻址错误会和实现一起错，测试变瞎。
2. **语义锚点是业界共识**：test_plan.py 的表述是「the reference built from all_to_all_single + permute, **which is the semantics every Ulysses implementation agrees on**」（[test/test_plan.py:L3-L7](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L3-L7)）；correctness.py 还注明这正是 sglang 的 usp.py 走的路径（[L29](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L29)）。位精确等于承诺「同一语义、不同传输」。
3. **它就是 baseline**：README/benchmark 里的 BASE 就是这条路径。正确性上与 BASE 位精确相等，性能对比（1.60–2.17×）才有意义。

分片不等时换参照：`dist.all_to_all` 列表版是 torch 在分片不等时唯一可表达的路径（[test/distributed/correctness.py:L44-L45](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L44-L45)）。

#### 4.1.2 核心流程

worker 的骨架是「一个不抛异常的 `Checks.equal` + 逐条 barrier + 末尾 all_reduce 汇总」：

```
init_process_group("nccl") → 构造 UlyssesGroup(require_nvlink=False)
for 每个维度:
    got  = 本库调用结果
    want = torch.distributed 参照结果
    Checks.equal(name, got, want)     # 形状不同→FAIL；逐元素不等→FAIL；rank 0 打 OK
    dist.barrier()                     # 每条检查后全员汇合
verdict = all_reduce(failed 计数)      # 任一 rank 失败→整体非零退出
```

correctness.py 的完整维度清单（本讲的「地图」，后面三个模块逐个展开重点）：

| 维度 | 判据 | 源码位置 |
| --- | --- | --- |
| 偶均分 sweep：fp16/bf16 × d∈{64,128,256,512} × 双 mode | `reference_even` 位精确 | [L94-L104](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L94-L104) |
| 不均分：近均（差 1 token）与极端倾斜 | `reference_uneven` 位精确 | [L106-L126](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L106-L126) |
| `out=` 三态：普通张量 / `empty_output` 缓冲（零拷贝）/ 默认分配 | 位精确 + **epoch 探针**验证走的是零拷贝路径 | [L128-L159](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L128-L159) |
| 拷贝结果能活过下一次调用 | 位精确 | [L155-L159](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L155-L159) |
| 往返：mode 1 撤销 mode 0 | `== x` 位精确 | [L161-L162](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L161-L162) |
| async：基本、非连续输入、`out=empty_output` | `.wait()` 后位精确 | [L164-L183](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L164-L183) |
| 5 个同形状 async 在飞 | 结果位精确 + **staging 探针**增长恰为 `[1,2,3,4,4]` | [L185-L219](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L185-L219) |
| `lend=True` 轮转 | 5 次调用的地址 = 4 个不同地址后回到第一个 | [L221-L249](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L221-L249) |
| 20 轮同窗口（不重分配） | **epoch 探针**增量恰为 `2*rounds` | [L251-L272](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L251-L272) |
| dtype 全表（4.2 展开） | 往返后按字节比较 | [L274-L294](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L274-L294) |
| backward（4.3 展开） | `xg.grad == forward(1-mode)` 位精确 | [L296-L325](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L296-L325) |
| forward AD / double backward | tangent 被搬运 / 二阶导恰为 2 | [L330-L358](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L330-L358) |
| async 离开默认流 | 带「压舱 GEMM」的非默认流上结果位精确 | [L360-L374](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L360-L374) |
| `empty_output` 返回普通张量 | `requires_grad/grad_fn/is_leaf` 全阴性 | [L376-L387](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L376-L387) |
| meta 形状传播 | FakeTensorMode 下只出形状 | [L389-L397](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L389-L397) |
| timed 变体搬运相同字节 | 位精确 + 四阶段有限、非负、和不超总时长且 transfer>0 | [L399-L442](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L399-L442) |

注意其中三条**不是**看结果、而是看探针（epoch_debug / staging_slots_debug / data_ptr 模式）——因为正确的结果证明不了「走的是哪条路径」「窗口有没有被重分配」「暂存池有没有真并发」，这正是 u9-l3「测试变盲」主题的前奏。

#### 4.1.3 源码精读

**偶均分参照**。mode 0 把 `(b, s_local, n_global, d)` 重排为 `(b, ws, s_local, n_global/ws, d)` 让 peer 维到 dim 0，`all_to_all_single` 均分交换，再 permute 回 4D：

```python
# reference_even 的 mode 0 分支（节选）
xt = x.view(b, s_local, ws, n_global // ws, d).permute(2, 0, 1, 3, 4).contiguous()
out = torch.empty_like(xt)
dist.all_to_all_single(out, xt, group=pg)
return out.permute(1, 0, 2, 3, 4).contiguous().view(b, ws * s_local, n_global // ws, d)
```

这是 [test/distributed/correctness.py:L28-L41](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L28-L41)，注释点名「sglang 的 usp.py 走的正是这条路径」。mode 1 分支是镜像（L37-L41）。

**不均分参照**。手工 narrow 出每个 peer 的切片、手工声明每个 peer 的接收形状、`dist.all_to_all` 列表版交换、按轴 cat 回去——rank 的接收形状用 `seq_splits[r]`（peer 的序列分片）与 `head_splits[rank]`（自己的头分片）组合，见 [test/distributed/correctness.py:L44-L63](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L44-L63)。这两个参照同时也是 test_plan.py 主机重放的锚（[test/test_plan.py:L72-L98](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L72-L98)），两处参照语义一致。

**检查器**。`Checks.equal` 三分支：形状不同、逐元素不等（`torch.equal`，统计不匹配元素数）、通过时仅 rank 0 打 OK；无论哪种路径都以 `dist.barrier()` 收尾：

- [test/distributed/correctness.py:L70-L80](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L70-L80)——`equal` 不抛异常，失败只计数；避免一个 rank 抛 Python 异常、其余 rank 卡在 barrier 里的不对称挂起。

**末尾汇总**：失败计数 `all_reduce` 后非零即整体 `SystemExit(1)`（[L444-L451](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L444-L451)），把「任一 rank 的失败」升级成「测试失败」。

**两个探针的用例**（结果证明不了的事）：

- `out=empty_output` 的零拷贝路径检测：返回对象指针相同是「构造上为真」，区分不出路径；epoch 是探测器——分配会清零 signal pad，只有真正走该缓冲的屏障才推进它，一次调用两道屏障，故期望恰为 2（[L137-L153](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L137-L153)）。
- 20 轮同窗口：结果正确推不出「没重分配」（每轮重分配的窗口也给对答案）；分配清零 pad，所以 ROUNDS 轮调用 epoch 恰好 +2·ROUNDS，中途重分配会让它变短（[L252-L272](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L252-L272)）。

#### 4.1.4 代码实践：给参照做一次「负控制」

1. **实践目标**：证明 correctness.py 的参照「有牙齿」——参照被改错时测试必须 FAIL，而不是永远 OK。
2. **操作步骤**：
   - 复制 `test/distributed/correctness.py` 为 `correctness_mut.py`（放在同一目录，仅作练习，勿提交）。
   - 把 `reference_even` 的 mode 0 分支里 `permute(2, 0, 1, 3, 4)` 改成 `permute(0, 2, 1, 3, 4)`（一个错误的中间排列）。
   - 运行：`torchrun --nproc_per_node=<N> test/distributed/correctness_mut.py`（N≥2，需要多卡）。
3. **需要观察的现象**：第一批 `even ... mode=0` 检查应打印 `FAIL ... elements differ` 或形状不匹配；末尾 `FAILED n checks` 且进程退出码非 0。
4. **预期结果**：mode 0 的偶均分检查全部 FAIL、mode 1 不受影响（参照的 mode 1 分支没动）、汇总非零。这验证了「位精确 + 独立参照」确实能抓排列错误。
5. 无多卡环境时：本实践**待本地验证**；可退而做纸面推演——写出 `permute(0,2,1,3,4)` 后 `view(b, ws*s_local, ...)` 是否仍形状合法，若直接形状崩溃说明失败会落在形状分支。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Checks.equal` 用「计数 + 打印」而不是直接 `assert`？
答案：worker 里每个 rank 都要走到 `dist.barrier()`；若失败 rank 直接抛异常退出，其余 rank 会在下一条 barrier 上永远等待，测试挂起而非失败（u9-l1 讲过的孤儿自旋问题）。计数让失败「带在身上」走到最后的 all_reduce。

**练习 2**：`out=empty_output` 那条检查为什么不能靠比较 `data_ptr` 证明走了零拷贝路径？
答案：`make_output` 返回的本来就是「交进去的那个对象」（按构造指针必然相等）；指针无法区分「peers 直写缓冲」与「先写内部窗口再 copy_out」两条路径——后者答案也对、也不报错。epoch 是唯一可见差异：只有走该缓冲的屏障才推进它，一次调用恰 +2（[L137-L141](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L137-L141) 的注释）。

**练习 3**：维度表里「拷贝结果能活过下一次调用」为什么只对 copying 形态检查（[L155-L159](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L155-L159)）？
答案：零拷贝结果就是单缓冲窗口的视图，**设计上**会被下一次调用覆写；对其断言「活过下一次调用」是把实现细节当契约。拷贝路径分配新张量，存活是它承诺的语义。

### 4.2 dtype 全表与「按字节比较」

#### 4.2.1 概念说明

dtype 维度要回答：**7 种白名单 dtype 在传输层真的只差一个 element size 吗？**关键直觉：拷贝引擎搬运的是**字节**，CopyOp 的所有字段（offset/pitch/width）都以字节计，传输不解析元素含义。因此 fp32/fp8/int8/uint8 与已位精确验证过的 fp16/bf16 的差别只在 `elem_size`。

两个技术障碍决定了这一节的形态：

1. `all_to_all_single` 走 NCCL，**不承载 float8**——绝对参照在多卡上拿不到 fp8 结果。
2. **排列的参照不需要懂字节含义**——于是把数据 `.view(torch.uint8)` 后逐字节比较，绕开 dtype 语义。

#### 4.2.2 核心流程

```
seed = randn(..., fp32)
xd   = seed.to(dtype)            # 整型走 (seed.abs()*50).to(dtype)
back = mode1(mode0(xd))          # 往返
Checks.equal(back.view(uint8), xd.view(uint8))   # 按字节位精确
```

为什么用往返而不是绝对参照？因为 fp8 无 NCCL 参照。为什么往返**够而不完美**？worker 自己写明了盲区：往返看不见**两个 mode 之间对称的寻址错误**（一个把字节搬错位置、另一个原样搬回来的错误对往返不可见）。所以注释补了一句兜底：一字节 dtype 的**绝对**参照是 test_plan.py 的重放——它按 dtype 参数化（[test/distributed/correctness.py:L274-L294](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L274-L294)）。

#### 4.2.3 源码精读

dtype 循环覆盖 fp32、fp8_e4m3fn、fp8_e5m2、int8、uint8（fp16/bf16 已在偶均分 sweep 里对绝对参照位精确过）：

- [test/distributed/correctness.py:L280-L294](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L280-L294)——整型输入用 `(seed.abs() * 50).to(dtype)` 造非平凡字节；比较前 `.view(torch.uint8)`。

「按字节比较」的合法性来自 dtype 白名单与对齐规则都在 make_dims 里一次性判定：

- [src/group.cc:L59-L62](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L59-L62)——白名单 7 种 dtype；注释说明这是**决策而非约束**（kBool、复数、量化类型未经推理，主动排除）。
- [src/group.cc:L67-L71](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L67-L71)——一条检查覆盖整个 plan：build_plan 发射的每个字节量都是 `d*elem` 的倍数，故只查 `(d*elem) % 16 == 0`；注释点名**规则随元素变小而收紧**：fp16/bf16 要求 `d%8`、fp32 要求 `d%4`、fp8/int8/uint8 要求 `d%16`。

validation.py 专门有一条验证这种「收紧」：8 个 bf16 元素 = 16 B 合法、同样 d=8 换成 fp8 = 8 B 即被拒（[test/distributed/validation.py:L103-L108](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/validation.py#L103-L108)）。

一字节 dtype 的绝对参照落在主机侧：test_plan.py 对 `np.float16/np.float32/np.int8/np.uint8` 参数化重放（[test/test_plan.py:L184-L193](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L184-L193)），其中 int8/uint8 正是「一字节元素尺寸」的 numpy 可表达集合；float8 没有 numpy 对照物，多卡上只有「按字节往返」这一层覆盖——这是文档写明的边界，不是疏漏。

#### 4.2.4 代码实践：主机上跑一字节 dtype 的绝对参照

1. **实践目标**：亲眼确认「dtype 只差 element size」在寻址层有绝对参照兜底，且不需要 GPU。
2. **操作步骤**：
   ```bash
   pytest test/test_plan.py -k "dtype or empty" -v
   ```
   然后阅读 [test/test_plan.py:L121-L148](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L121-L148) 的 `check()`：它用 `np.dtype(dtype).itemsize` 作为 elem_size 调 `a2a_plan_debug`（[L27-L37](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L27-L37)），重放 `apply_ops`（[L40-L59](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L40-L59)）后与 `reference_scatter_head` 比对。
3. **需要观察的现象**：int8/uint8（以及 fp16/fp32）参数化的测试全部通过；测试收集阶段若 `_C` 未构建会整层 skip（[L19-L21](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L19-L21)）。
4. **预期结果**：全绿（或干净 skip）。再自问：为什么这里能用 fp16 绝对参照而多卡 correctness 对 fp8 只能往返？——NCCL 不载 fp8，而 numpy 重放不经过任何通信库。
5. 本实践**待本地验证**（取决于 `_C` 是否已按 u1-l3 构建）。

#### 4.2.5 小练习与答案

**练习 1**：`d=8` 的 bf16 输入合法、`d=8` 的 fp8 输入非法，为什么？
答案：对齐规则是 `(d * elem_size) % 16 == 0`：8×2=16 B 合法；8×1=8 B 不合法。元素变小时同一 d 的字节数减半，规则「收紧」。这正是 validation.py L103-L108 那条检查的用意。

**练习 2**：往返比较的盲区是什么？项目用什么补？
答案：两个 mode 间**对称**的寻址错误（搬错位置又原样搬回）对往返不可见。补法：偶均分下 fp16/bf16 对 all_to_all_single 的绝对位精确（sweep 段）+ 一字节 dtype 在 test_plan.py 的主机重放绝对参照；fp8 在多卡上只有往返覆盖，是文档写明的边界。

**练习 3**：为什么造 int8 输入用 `(seed.abs() * 50).to(torch.int8)` 而不是直接 `randn(...).to(torch.int8)`？
答案：直接转换会把大部分值截断成 0/±1，字节模式高度重复，搬运错误容易「碰巧相等」；放大 50 倍制造非平凡、互不相同的字节，让逐字节比较有分辨力（[L287-L288](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L287-L288)）。

### 4.3 backward 不交换断言：mode 1 是 mode 0 的逆映射

#### 4.3.1 概念说明

backward 断言回答：**自动微分的梯度到底是不是「同族字节搬运的逆」？**fast-ulysses 的交换是纯排列（permutation）：输出是把输入的每个元素按固定规则挪到新位置，数值不变。排列的雅可比是置换矩阵 \(P\)，而置换矩阵的转置即逆：

\[ P^{-1} = P^{\top} \]

reverse-mode AD 的 vjp 是 \(P^{\top} g\)——**再执行一次逆排列**。对 4D all-to-all，逆排列恰好是另一个 mode 的前向：所以 backward 就是 `1 - mode` 的前向调用，位精确、不重算。

关键推论是 **splits 不交换**。`all_to_all_single` 的 backward 确实要互换 send/recv split sizes——因为那些数描述「本次调用每个 peer 的收发量」，方向反了角色就反了。而 `seq_splits[p]`/`head_splits[p]` 描述的是**组的所有权几何**（rank p 拥有哪段序列、哪段头），数据怎么流它都成立；`mode` 只选择「哪根轴到达时已被分片」。源码注释把这段对比写得很直白（[src/bindings.cc:L500-L507](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L500-L507)）。

#### 4.3.2 核心流程

correctness.py 的 backward 检查分两步：

1. **偶均分**：`xg.grad` 与 `group.all_to_all_4d(upstream, mode=1-mode)` 位精确相等（[L299-L308](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L299-L308)）——这验证 vjp = 逆排列。
2. **倾斜 splits 原样传递**：用 `[s_total-(ws-1), 1, ..., 1]` 的极端倾斜表做 backward，断言梯度等于**同一对 splits** 的 mode 1 前向（[L310-L325](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L310-L325)）。注释点明用意：若有人按 all_to_all_single 的习惯「修」backward、把两张表交换，这个检查会失败——而且在倾斜切分下**先败于形状**，不只是数值。

实现侧的链条：A2AFunction 的 forward 把 group、mode、两张表存进 `saved_data`（`IValue` 用 None 保留「未传」语义）；backward 原样取出，调 `apply(group, grads[0].contiguous(), 1 - mode, seq, head)`——见 [src/bindings.cc:L523-L528](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L523-L528) 与 [L541-L556](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L541-L556)。走 `apply()` 而非裸函数，让 grad-of-grad 递归建图（double backward 检查在 [L348-L358](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L348-L358)）。

#### 4.3.3 源码精读：build_plan 里的互逆证明

记排他前缀和 \(S_r = \sum_{i<r} \text{seq\_splits}[i]\)（rank r 的序列分片在全序列中的偏移）、\(H_p = \sum_{i<p} \text{head\_splits}[i]\)（p 的头分片偏移）。两分支的拷贝语义（[src/a2a_plan.cc:L176-L213](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L176-L213)）：

**mode 0（rank r 发给 peer p）**：源是本 rank 的序列行、p 的头列；目的是 p 的窗口里「本 rank 序列分片」那段、列从 0 起：

\[ x_r[\,b,\ s,\ H_p + h\,] \;\longmapsto\; W_p[\,b,\ S_r + s,\ h\,] \]

对应字段：`src_offset = H_p·d_bytes`（源从 p 的头列起）、`src_pitch = head_total·d_bytes`（源行距 = 满头行宽，读跨步）、`dst_offset = S_r·row`（目的落在本 rank 序列偏移处）、`dst_pitch = width`（目的行距 = 行宽，写连续）、`rows = s_me`、`width = head_splits[p]·d_bytes`——[L180-L192](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L180-L192)。

**mode 1（rank p 发给 peer r）**：源是 p 的窗口/输入里 r 的序列行、p 自己的头列；目的是 r 的窗口里 r 的序列行、全局头列从 \(H_p\) 起：

\[ x_p[\,b,\ S_r + s,\ h\,] \;\longmapsto\; W_r[\,b,\ s,\ H_p + h\,] \]

对应字段恰好互换：`src_offset = S_r·row`、`src_pitch = width`（读连续）、`dst_offset = H_{me}·d_bytes`、`dst_pitch = head_total·d_bytes`（写跨步）、`rows = s_p`、`width = n_me·d_bytes`——[L199-L212](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L199-L212)。

两个映射复合：

\[
x_r[b, s, H_p{+}h] \xrightarrow{\ \text{mode 0}\ } W_p[b, S_r{+}s, h]
\xrightarrow{\ \text{mode 1}\ } W_r[b, s, H_p{+}h] = x_r[b, s, H_p{+}h]
\]

即 **mode 1 ∘ mode 0 = 恒等**（对每个 (r, p) 对都成立，遍历所有 peer 即整体互逆）。这就是 correctness.py「round trip」检查（[L161-L162](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L161-L162)）与 backward 断言共同的数学基础。

为什么**同一对 splits** 服务两个方向？看偏移公式：mode 0 用「p 拥有头段 \(H_p\)」算**源**侧、用「r 拥有序列段 \(S_r\)」算**目的**侧；mode 1 把同一对事实换到对侧（源用 \(S_r\)、目的用 \(H_p\)）。所有权表没变，变的只是哪一侧取跨步——所以 backward 原样传表即可，交换反而破坏逆映射（倾斜表下连 make_dims 的形状交叉校验都过不了，[src/group.cc:L88-L109](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L88-L109)）。

#### 4.3.4 代码实践：用 a2a_plan_debug 亲眼看到镜像

1. **实践目标**：在无 GPU 环境把 4.3.3 的推导落到可见的 ops 表上。
2. **操作步骤**：写一个小脚本（**示例代码**，非项目文件），参照 [test/test_plan.py:L27-L37](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L27-L37) 的调用方式：

   ```python
   import torch, fast_ulysses  # 注册 torch.ops.fast_ulysses
   splits = ([9, 4, 6, 1], [4, 1, 3, 2])            # seq_splits, head_splits
   for (r, p) in [(0, 1), (1, 0)]:
       m0 = torch.ops.fast_ulysses.a2a_plan_debug(2, 5, r, 4, *splits, 0, 2)
       m1 = torch.ops.fast_ulysses.a2a_plan_debug(2, 5, p, 4, *splits, 1, 2)
       # 取 m0.ops 中 peer==p 的行 与 m1.ops 中 peer==r 的行，逐字段对照
   ```

   对照 `rank r 的 mode-0（peer p）` 与 `rank p 的 mode-1（peer r）` 两条 op：应有 `src_offset ↔ dst_offset`、`src_pitch ↔ dst_pitch`、`width` 相等、`rows` 相等（分别为 `seq_splits[r]` 与 `head_splits` 换位后的同一行数）。
3. **需要观察的现象**：两条 op 的 (width, rows) 相同；mode 0 的 (src_offset, src_pitch) 与 mode 1 的 (dst_offset, dst_pitch) 按上文公式对应。
4. **预期结果**：镜像关系成立；`output_shape` 互为对方输入形状。若想更进一步，用 [test/test_plan.py:L40-L59](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L40-L59) 的 `apply_ops` 在 numpy 缓冲上先后重放两个 plan，验证数据还原（这正是 test_plan.py 往返测试的做法）。
5. 本实践需要构建好的 `_C` 扩展（无需 GPU 与进程组）；未构建时**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么倾斜 splits 下「交换两张表的 backward」会**先败于形状**而不是给出错误数值？
答案：make_dims 交叉校验输入形状与 splits 推出的期望形状（[src/group.cc:L88-L109](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L88-L109)）。交换后，rank r 的 backward 输入（梯度）形状按错误的表推导，x1/x2 对不上即 TORCH_CHECK 失败——这正是 correctness.py 选倾斜表做断言的原因：错误暴露得最响。

**练习 2**：backward 为什么永远走 copying 路径、不进零拷贝？
答案：backward 由 autograd 引擎发起；零拷贝需要的集合窗口分配会把 rendezvous 时机交给引擎的节点顺序而非用户程序，且梯度会被引擎累积/持有，而窗口是单缓冲（[src/bindings.cc:L549-L552](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L549-L552) 注释）。

**练习 3**：forward AD（jvp）为什么也「只是再排一次列」？
答案：\(P\) 是线性映射本身，\(P(x + \varepsilon t) = Px + \varepsilon Pt\)，tangent 走同一排列——即第二条集合通信。correctness.py 断言 `jvp == forward(tangent)`（[L330-L346](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L330-L346)）；若无手工搬运，tangent 会被静默丢弃、dual 返回 None——该检查防的正是这个。

### 4.4 拒绝消息断言：validation.py 与「先于第一个握手」

#### 4.4.1 概念说明

correctness.py 只喂合法参数；**文档承诺的每一条拒绝**由 validation.py 兑现。它的开篇说明了存在的理由：docs/api.md 列了 Raises，但 test_plan.py 手工构造 A2ADims、从不经过 make_dims/prepare，correctness.py 只给合法参数——**整个拒绝面此前零覆盖**（[test/distributed/validation.py:L1-L8](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/validation.py#L1-L8)）。

三条断言纪律：

1. **必须 raise**——「accepted」就是失败（拒绝面收窄是回归）。
2. **消息必须含指定片段**——消息是契约的一部分：用户按文档排错、CI 按文案归因。只查「抛了异常」会让消息漂移无人知晓。
3. **每条检查后全员 barrier**——这是「验证先于第一个握手」的检验机制本身。若某拒绝只在部分 rank 发生、或发生在开路屏障之后，rank 会在这里失步：worker **挂起**而不是报 FAIL。挂起会被 torchrun 包装器的超时三段式（u9-l1）转成失败。换言之，这条性质没有单独的断言，它由「worker 能跑完」整体证明（[L9-L11](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/validation.py#L9-L11) 与 [L62-L64](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/validation.py#L62-L64) 的注释）。

#### 4.4.2 核心流程

```
Rejects.raises(name, fragment, fn):
    try: fn()
    except: 消息不含 fragment → FAIL（"raised, but not for the stated reason"）
    else:   → FAIL（"accepted"）
    dist.barrier()          # 全 rank 一致且没把同伴留在握手里

Rejects.accepts(name, fn):  # 拒绝必须「不留后遗症」
    fn() 抛任何异常 → FAIL；否则 OK
    dist.barrier()
```

覆盖面按主题分组：形状/dtype/mode（L91-L110）、splits（L112-L129）、空集合通信（L131-L141）、`out=`（L143-L161）、别名守卫（L163-L173）、autograd 拒绝（L175-L192）、vmap 七连（L194-L267）、plan 缓存投毒（L269-L278）、跨设备（L280-L310）、inference mode（L312-L324）、lend 边界（L325-L348）、销毁后的组（L359-L383）。

#### 4.4.3 源码精读

**检查器本体**：[test/distributed/validation.py:L49-L64](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/validation.py#L49-L64)。注意它**故意不匹配异常类型**（`except Exception`，L53 的 noqa 注释写着「the type is torch's, the message is the contract」）——同一条拒绝在 Python 层是 `RuntimeError`、在 C++ 层经 TORCH_CHECK 也映射为 RuntimeError，但文案才是稳定契约。

**消息片段 → 源码产地**（下表是本模块的「对账单」，也是 4.4.4 实践的底稿）：

| fragment | 产地 | 运行时机 |
| --- | --- | --- |
| `must be 4D` / `dtype must be` / `16-byte aligned` / `mode must be 0 or 1` | make_dims：[src/group.cc:L59-L72](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L59-L72) | plan 之前 |
| `both seq_splits and head_splits` / `but the splits imply` / `must divide world_size` | make_dims 的 splits 段：[src/group.cc:L80-L135](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L80-L135) | plan 之前 |
| `moves no data` | allocate：[src/group.cc:L212-L214](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L212-L214)（窗口分配前，全 rank 同掷） | 集合分配入口 |
| `out has shape` / `out has dtype` / `contiguous CUDA tensor` / `out is on cuda:` | prepare 的 out 检查：[src/bindings.cc:L112-L129](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L112-L129) | 第一道屏障前 |
| `input overlaps the window` | 字节区间重叠守卫：[src/bindings.cc:L158-L172](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L158-L172) | 第一道屏障前 |
| `does not support autograd`（out=/async） | Python 前置：[python/fast_ulysses/group.py:L133-L143](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L133-L143)、[L188-L197](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L188-L197) | 进 C++ 之前 |
| `not both`（out=+lend） | Python 前置：[python/fast_ulysses/group.py:L180-L187](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L180-L187) | 进 C++ 之前 |
| `does not support torch.vmap` | 只抛错的 boxed 内核：[src/bindings.cc:L58-L67](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L58-L67) | 进通信之前 |
| `one group serves exactly one device` | require_group_device：[src/bindings.cc:L38-L48](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L38-L48) | 第一道屏障前 |
| `lent results may be alive at once` | 轮转落回存活槽：[src/group.cc:L289-L296](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L289-L296) | 集合分配之前（槽由调用计数选，全 rank 一致） |
| `has been destroyed` | check_alive：[src/group.cc:L162-L165](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L162-L165) | 任何握手之前 |

**「先于屏障」的三个精妙用例**：

- **plan 缓存投毒**（[L269-L278](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/validation.py#L269-L278)）：先用偶均分调用暖缓存，再传空 splits 列表，断言仍拒绝（`seq_splits has 0 entries`，产地 [src/a2a_plan.cc:L124-L127](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L124-L127)）。这是真实 bug 的回归测试：PlanKey 曾把「没传」与「传空」折叠成同一个键，第二次调用继承了第一次的 plan 而不是自己的拒绝——修复正是把 optional 保进键里（[src/group.cc:L173-L180](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L173-L180) 注释）。
- **跨设备投毒 + accepts**（[L280-L310](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/validation.py#L280-L310)）：错误设备的调用被拒后，必须验证同形状的后续调用**还能用**（`check.accepts`）——因为暂存池按 (shape, dtype) 缓存，一次错误设备的分配会把该形状永久毒化。
- **销毁后的组**（[L359-L383](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/validation.py#L359-L383)）：零拷贝路径不经过 window()/make_output——曾经漏检 destroyed 标志，整场集合通信跑在已销毁的组上还重建了刚拆掉的传输流。三条入口（copying / zero-copy / async）各查一遍。

**vmap 七连的布局**（[L194-L267](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/validation.py#L194-L267)）：六个入口走 functorch 的 FuncTorchBatched 键，第七个走**遗留 Batched 键**（`torch.autograd.grad(is_grads_batched=True)`，L251-L267）。注释点明：只注册 functorch 键时前六条照常通过、**静默回退成按 batch 逐元素循环**的恰是 backward——各 rank 集合通信次数失配、无报错挂起。断言的是「raise」而非「不挂」，因为挂起无法被 CI 观察。

**文档化的「测不到」**：worker 的 docstring 专门写下 prepare() 里**两条不可达守卫**及原因（窗口字节数 vs 调用所需的两处比较，[src/bindings.cc:L130-L138](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L130-L138) 即其中之一）：形状检查先拦小缓冲、异 dtype 视图构造不出来、构造出来也是 rank 依赖的（会挂而非报）——它们防的是未来调用方，不是现存调用方（[L19-L32](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/validation.py#L19-L32)）。把「为什么测不到」写进测试，是防止后人误判为疏漏。

#### 4.4.4 代码实践：消息溯源（无 GPU 可做）

1. **实践目标**：把 4.4.3 对账单的「产地」一栏自己填一遍，确认每条拒绝都发生在第一个握手之前。
2. **操作步骤**：
   - 打开 [test/distributed/validation.py:L91-L161](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/validation.py#L91-L161)，对每条 `check.raises` 的 fragment，用 Grep 在 `src/` 里找产出行。
   - 对每一处在产地旁标注：它在 `prepare()` 的哪一步（对照 u6-l2 的校验链：check_alive → prune_owned → require_cuda → device → make_dims/plan → out/lend → 重叠检查）。
   - 结论应满足：**所有产地都在校验链内，即都在开路屏障之前**；唯一例外是 `moves no data`，它在 allocate() 入口、也在任何 rendezvous/屏障之前，且因 window_numel 全 rank 相同而全 rank 同掷。
3. **需要观察的现象**：没有一条拒绝消息的产地位于 `transfer_on_stream` 或屏障之后。
4. **预期结果**：完成一张 fragment → 文件:行 → 校验链位置 的三列表；这与 u6-l2 的「全部校验先于第一个屏障」设计原则逐条对上。
5. 纯阅读实践，可直接完成；如想运行 worker：`torchrun --nproc_per_node=<N> test/distributed/validation.py`（需 ≥2 卡，**待本地验证**）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `raises` 匹配消息片段而不匹配异常类型？
答案：消息是文档承诺的契约（docs/api.md Raises 逐条对应文案）；异常类型由 torch 的机制决定（TORCH_CHECK 与 Python raise 可能统一映射），且不同入口（Python 前置 vs C++）类型本就可能不同。锁死类型会把契约绑到实现细节上（L53 的 noqa 注释明说「the message is the contract」）。

**练习 2**：`check.accepts` 存在的价值是什么？举两例。
答案：验证「拒绝没有留后遗症」。例一：错误设备的 async 调用被拒后，同形状的合法调用必须仍可用——暂存池按 (shape, dtype) 缓存，曾被毒化（L299-L303）。例二：inference_mode 内的 async 调用被接受后，模式外的同形状调用也必须可用——暂存缓冲若在 inference mode 下分配会成为 inference tensor，令后续 copy_ 全部失败（L312-L324）。

**练习 3**：vmap 的失败模式为什么是「挂起」而非报错？库如何把挂起变成报错？
答案：vmap 无 batching rule 时静默回退为按 batch 元素的 for 循环——每个元素发一次集合通信；各 rank batch 大小不同则通信次数不同，先发完的 rank 在屏障里等永远不来的同伴，无异常无超时。库在 FuncTorchBatched 与遗留 Batched 两个 dispatch key 上注册只抛错的内核（u6-l4），把挂起前置成显式 RuntimeError；validation.py 断言七个入口都 raise。

## 5. 综合实践

本讲综合实践对应任务书两问，全部可在**无 GPU** 环境完成（需要已构建 `_C`）。

### 任务 A：从断言追到 build_plan 的互逆证明

1. 定位断言：[test/distributed/correctness.py:L310-L325](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L310-L325)——倾斜 splits 的 backward 检查，名字就叫 `backward with skewed splits, passed through`。读 L311-L312 的注释：「这是那个会在有人按 all_to_all_single 的方式交换 splits『修复』backward 时失败的检查——而且在倾斜切分下败于形状，不只是数值」。
2. 追实现链：[src/bindings.cc:L541-L556](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L541-L556)——backward 从 saved_data 原样取出 seq/head，调 `apply(group, grads[0], 1 - mode, seq, head)`；再读 [L500-L507](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L500-L507) 的注释（排列矩阵转置即逆、splits 是组属性）。
3. **写出数学依据**（参考答案，读者应先自行推导再核对）：按 4.3.3 的记号，mode 0 把 \(x_r[b,s,H_p{+}h]\) 映到 \(W_p[b,S_r{+}s,h]\)，mode 1 把 \(W_p[b,S_r{+}s,h]\) 映回 \(W_r[b,s,H_p{+}h]\)；两映射复合为恒等，故 mode 1 = mode 0⁻¹，即 \(P^{\top}=P^{-1}\)，vjp 是逆排列、backward 是 `1-mode` 前向。两张 splits 表给出 \(S_r\) 与 \(H_p\)——它们编码**所有权**，mode 0 与 mode 1 只是把它用在拷贝的 different 侧（源偏移 vs 目的偏移），因此 backward 原样传表；交换等于改写所有权，逆映射失效。
4. 数值验证：用 4.3.4 的 `a2a_plan_debug` 脚本，对 `seq_splits=[9,4,6,1]`、`head_splits=[4,1,3,2]` 打印全部 rank 的两个 plan，用 [test/test_plan.py:L40-L59](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L40-L59) 的 `apply_ops` 在 numpy 上先 mode 0 后 mode 1 地重放，断言字节还原。再「作弊」一次：把 mode 1 重放用的表交换成 `seq=[4,1,3,2]`、`head=[9,4,6,1]`，观察它要么在形状上崩掉、要么数据不复原——这就是断言抓的错误。

### 任务 B：为 validation.py 设计新非法用例

按下表设计（消息产地均已核对），并遵守两条纪律：**全 rank 一致**、**先于第一个握手**。

| 用例 | 调用 | 预期消息片段 | 产地 | 为何合格 |
| --- | --- | --- | --- | --- |
| mode=3 | `group.all_to_all_4d(good, mode=3)` | `mode must be 0 or 1` | [src/group.cc:L72](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L72) | make_dims 在 plan/屏障前；mode 是标量，全 rank 同值 |
| 负分片 | `group.all_to_all_4d(good, seq_splits=[-1]*ws, head_splits=[4]*ws)` | `is negative` | [src/a2a_plan.cc:L133-L140](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L133-L140)（经 [src/group.cc:L87](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L87) 的 `dims.validate()`） | validate 在索引 rank 之前、任何分配之前 |
| 表长错误 | `seq_splits=[16]*(ws+1), head_splits=[4]*(ws+1)` | `entries, expected` | [src/a2a_plan.cc:L124-L131](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L124-L131) | 同上 |
| world_size 超界 | 用 9 进程 torchrun 启动 worker（构造期触发） | `world_size must be in [1, 8]` | [src/group.cc:L151-L153](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L151-L153) | 构造期拒绝：每个 rank 在同一行同时失败，天然一致且先于一切 |

可直接粘贴的片段（**示例代码**，加在 validation.py 的 splits 段之后即可）：

```python
check.raises("mode 3", "mode must be 0 or 1", lambda: group.all_to_all_4d(good, mode=3))
check.raises(
    "a negative split",
    "is negative",
    lambda: group.all_to_all_4d(good, seq_splits=[-1] * ws, head_splits=[4] * ws),
)
```

注意两个细节：(1) mode=3 与现有 mode 2 检查**几乎重复**——真正有信息量的是负分片与表长错误（覆盖 `A2ADims::validate` 的不同分支）；(2) `seq_splits=[-1]*ws` 会在形状交叉校验**之前**的 `dims.validate()`（[src/group.cc:L87](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L87)）被拦下，所以预期消息是 `is negative` 而不是 `but the splits imply`——想测后者请用「合法但矛盾」的表（现有 L118-L122 已覆盖）。以上运行效果**待本地验证**（需 ≥2 卡跑 worker；消息文案可离线核对）。

## 6. 本讲小结

- correctness.py 的主轴是**位精确对照独立参照**：偶均分对 `all_to_all_single`+permute（业界共识语义、正是 baseline 路径），不均分对 `dist.all_to_all` 列表版——「close enough」在任何地方都不是通过。
- 结果正确证明不了路径正确：零拷贝是否真的零拷贝（epoch 恰 +2）、窗口是否 20 轮不重分配（epoch 恰 +2·ROUNDS）、暂存池是否真并发（增长恰 `[1,2,3,4,4]`）、lend 是否真轮转（4 地址回绕）都要靠探针。
- dtype 维度靠「按字节比较的往返」覆盖 fp8（NCCL 不载 fp8），其对称错误盲区由 test_plan.py 的主机重放绝对参照（含 int8/uint8 参数化）兜底；对齐规则 `(d*elem)%16==0` 随元素变小而收紧。
- backward 的数学：交换是排列，\(P^{-1}=P^{\top}\)，故 backward 是 `1-mode` 前向；splits 描述组所有权（\(S_r\)、\(H_p\) 的来源），mode 只决定它用在拷贝的哪一侧——所以**原样传递、绝不交换**，倾斜表下的断言会让「交换式修复」先败于形状。
- validation.py 把 docs/api.md 的 Raises 变成可执行契约：必须 raise、消息必须含片段、每条后全员 barrier；「验证先于第一个握手」不是单条断言，而是由 worker 能否活着跑完来整体证明——违反者以挂起的方式暴露，被超时三段式收割。
- 「测不到」也被文档化：prepare() 的两条字节守卫为何不可达、vmap 为何必须断言 raise 而非不挂——写下「为什么」，防止后人把边界误判为疏漏。

## 7. 下一步学习建议

下一讲 **u9-l3 对抗性测试：CE 顺序、窗口竞态与重叠屏障**沿着本讲提出的「负控制」思想继续深入：correctness/validation 证明「正确时正确」，u9-l3 证明「测试自己还能失败」——`_set_ce_fault` 武装的负控制、传输长度内的 skew 设计、单缓冲窗口的读写竞态。建议先消化本讲的探针三件套（epoch/staging/data_ptr 模式），它们是读懂对抗性测试的前置语言；之后 u9-l4 转向基准方法论（stages/overlap/zerosm 模式恰以本讲的 timed 检查为底），u9-l5 回到 design.md 的假设复盘。源码阅读上，可对照重读 [src/bindings.cc 的 prepare() 校验链](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L100-L174)与本讲的对账单，检验自己能独立说出每条拒绝的时机。
