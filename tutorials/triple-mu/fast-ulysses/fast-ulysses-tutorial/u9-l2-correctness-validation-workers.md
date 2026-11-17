# u9-l2 correctness 与 validation 工作负载

## 1. 本讲目标

学完本讲，你应该能够：

1. 完整列出 `correctness.py` 覆盖的维度清单（双模式、偶/不均分、out 三态、async、往返、20 轮同窗口、全部 dtype、backward、forward AD、meta、timed），并说出每个维度「为什么结果本身证明不了、要靠探针」。
2. 解释为什么位精确对照选择 `all_to_all_single + permute` 作为参照，以及为什么全文件任何一处都不接受 "close enough"。
3. 从 `correctness.py` 中找出「backward 的 splits 原样传递」的断言，并一路追到 `build_plan` 里 mode 1 是 mode 0 逆映射的数学依据，把它写出来。
4. 说明 `validation.py` 如何同时检验「每条文档化拒绝都 raise」与「拒绝发生在第一个握手之前」——后者的失败模式是挂起而不是失败。

## 2. 前置知识

- **位精确（bit-exact）与容差对照的区别**：u2-l4 的 DiT 示例里用 `allclose`（容差 5e-2）对照单卡参照，因为 SDPA 内部有浮点归约、顺序不同则末位不同。本讲的被测对象是**纯数据搬运**——不做一个浮点运算，只搬字节——所以「差不多」不是通过，是 bug 被容差藏住了。位精确用 `torch.equal`（逐元素相等）而不是 `allclose`。
- **参照（reference / oracle）测试**：给同一份输入，用一个独立实现的路径算出「应得」的结果，再与被测路径逐位比对。参照必须与被测代码**没有共享实现**，否则错得一致也测不出来。
- **worker 与启动壳**（u9-l1）：`test/distributed/` 下每个 `.py` 是独立的 torchrun worker；`test/test_distributed.py` 只是把这些 worker 当子进程跑的壳。本讲的两个 worker `correctness.py` 与 `validation.py` 都在 ≥2 卡时运行，也在 3 卡（非 2 幂）时运行。
- **「校验先于第一个屏障」**（u2-l2、u6-l2）：一次集合调用里，任何可能抛错的检查必须在第一道屏障之前完成，否则单 rank 报错会把其余 rank 留在 GPU 自旋里挂起。`docs/api.md` 的 Raises 一节承诺的就是这个时序。本讲的 `validation.py` 就是这条承诺的测试。
- **探针术语回顾**：`epoch`（u5-l3：设备侧每道屏障 atomicAdd 加 1，分配新窗口会把 signal pad 清零）、staging pool 与 `staging_slots_debug`（u7-l2）、`lend=True` 的四窗口轮转（u4-l4）、`a2a_plan_debug` 的无 GPU 重放（u3-l3、u9-l1）。本讲会看到它们全部作为「结果之外的观察维度」出现。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [test/distributed/correctness.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py) | 位精确正确性 worker：对 torch.distributed 路径逐位对照，覆盖全部调用形态 |
| [test/distributed/validation.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/validation.py) | 拒绝路径 worker：断言每条文档化拒绝都 raise、消息符合、且先于任何集合操作 |
| [src/a2a_plan.cc](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc) | 纯主机寻址层：4.3 节推导 mode 1 = mode 0⁻¹ 的地方 |
| [src/group.cc](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc) | `make_dims` 与构造函数的 `TORCH_CHECK`——validation 断言的消息来源 |
| [test/test_plan.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py) | 无 GPU 布局测试：本讲实践环节要复用它的 `build_plan`/`apply_ops`/`run_plan` 重放机制 |
| [test/test_distributed.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_distributed.py) | 启动壳：worker 清单、世界规模参数化、多卡跳过规则 |
| [docs/api.md](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md) | Raises 文档：validation 断言的消息契约就钉在这份清单上 |

## 4. 核心概念与源码讲解

### 4.1 位精确参照：为什么是 all_to_all_single + permute

#### 4.1.1 概念说明

`correctness.py` 的第一行 docstring 就立了规矩：**「Pure data movement, so "close enough" is not a passing result anywhere in this file」**——全文件没有任何一处用容差比较（[test/distributed/correctness.py:L1-L10](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L1-L10)）。

参照路径的选择有三条理由：

1. **独立性**：参照用 `torch.distributed` 的 `all_to_all_single` / `all_to_all`（NCCL 走 SM 的通用路径）加本地 `permute`/`view` 组合出来，与 fast-ulysses 的 CE 拷贝步长实现**零共享代码**。两边各自从同一输入算出结果，逐位相等才有意义。
2. **现实锚定**：docstring 明说这条 permute→all_to_all→permute 路径「the path sglang's usp.py takes」——它是业界现成 Ulysses 实现的真实做法，不是为测试发明的假想语义。
3. **覆盖两端**：偶均分用 `all_to_all_single`（张量形态、要求整除），不均分用列表版 `dist.all_to_all`（「the only shape torch takes when the shards differ」）——恰好是 torch 生态里两种集合通信的官方形态。

#### 4.1.2 核心流程

`Checks.equal` 是全文件的裁判，三段式：

```text
equal(name, got, want):
  1. 形状不同            → FAIL（打印两个 shape）
  2. torch.equal 逐位不等 → FAIL（打印多少个元素不同）
  3. rank 0 打印 OK
  最后 dist.barrier()   ← 每个 check 之后全组对齐
```

两个参照函数的语义（以 mode 0 为例）：

- `reference_even(x, 0, ws, pg)`：把 \((b, s_{local}, n_{global}, d)\) 先 `view` 成 \((b, s_{local}, ws, n_{global}/ws, d)\)、`permute` 把 ws 轴提到最前，`all_to_all_single` 交换后 `permute` 回来 `view` 成 \((b, ws \cdot s_{local}, n_{global}/ws, d)\)。
- `reference_uneven(x, 0, seq_splits, head_splits, rank, pg)`：用排他前缀和 `off = [0] + accumulate(splits)` 算出每个 peer 的头段，`narrow` 出 ws 个发送块，`dist.all_to_all` 收发，再按序列轴 `cat`。

#### 4.1.3 源码精读

偶均分参照——sglang 同款路径（[test/distributed/correctness.py:L28-L41](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L28-L41)）：mode 0 分支在 L31-L36，先 view/permute 把头轴按 rank 切开、`all_to_all_single` 换成序列分片、再 permute/view 还原成 4D；mode 1 分支（L37-L41）完全镜像。

不均分参照——torch 只接受列表形态（[test/distributed/correctness.py:L44-L63](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L44-L63)）：L46 的 `off` 用 `itertools.accumulate` 做排他前缀和；L49 按 mode 选被切轴（mode 0 切头轴、mode 1 切序列轴）；L50-L62 构造逐 peer 的 `send`/`recv` 列表；L63 按 mode 选 `cat` 的轴。注意 L54-L55 的收方形状用 `r if mode == 0 else rank` 选下标——mode 0 时我收到的是「peer r 的序列段 × 我的头」，mode 1 时是「我的序列段 × peer r 的头」。

裁判本体（[test/distributed/correctness.py:L66-L80](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L66-L80)）：L74 的 `torch.equal(got, want)` 是位精确判据；L77 在不等时统计并打印差异元素个数（比一个布尔假信息量大得多）；L80 的 `dist.barrier()` 保证一个 rank 失败不会让其余 rank 冲进下一个用例造成级联误报。

收尾裁决（[test/distributed/correctness.py:L444-L451](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L444-L451)）：本地失败计数 `all_reduce` 求和，rank 0 打印 `ALL PASS` 或失败数，非零则以 `SystemExit(1)` 退出——这是启动壳判定 worker 通过与否的信号。

#### 4.1.4 代码实践（源码阅读 + 纸面推演，无需 GPU）

1. **实践目标**：确认你真的读懂了 `reference_uneven` 的收发几何，而不是「大概明白」。
2. **操作步骤**：取 \(ws=3\)，`seq_splits=[2,1,3]`，`head_splits=[1,2,1]`，设 `rank=1`、`mode=0`、输入形状 \((1, 1, 4, 8)\)（注意 mode 0 下本 rank 的序列长是 `seq_splits[1]=1`、头数是全组 `sum(head_splits)=4`）。在纸上写出：
   - `off(seq_splits)` 与 `off(head_splits)`；
   - `send` 列表里 3 个张量的形状（提示：mode 0 沿头轴切，`axis=2`，每块是 `(b, s_me, h_p, d)`）；
   - `recv` 列表里 3 个张量的形状与最后 `cat(dim=1)` 的结果形状。
3. **需要观察的现象**：`send[p]` 的形状中头段来自 `head_splits[p]`，而 `recv[r]` 的形状中序列长来自 `seq_splits[r]`——发送侧看对方的头表、接收侧看对方的序列表。
4. **预期结果**：`recv` 形状为 `(1, 2, 2, 8)`、`(1, 1, 2, 8)`、`(1, 3, 2, 8)`，`cat` 后 `(1, 6, 2, 8)`——即 `output_shape = (b, seq_total, head_splits[rank], d)`，与 4.3 节 `build_plan` 的 `output_shape` 完全一致。
5. 本步为纸面推演，无运行结果，结论可用 `a2a_plan_debug`（见 4.3.4）核对。

#### 4.1.5 小练习与答案

**练习 1**：为什么参照不直接用 fast-ulysses 自己的 mode 0 去验证 mode 1（往返测试）就行？
**答案**：往返只能证明两个 mode 互逆，不能证明它们实现了约定的语义。一个「对称地搬错」的实现（mode 0 把 1、2 号 peer 的头段装反、mode 1 用同样的错位读回）往返照样逐位还原。绝对参照必须在语义上独立于被测实现——这就是 `all_to_all_single + permute` 的角色（4.2.4 的实验会把这个陷阱演示出来）。

**练习 2**：`Checks.equal` 里形状不等与逐位不等分成两个分支打印，有什么好处？
**答案**：形状错说明**寻址/形状推导**错了（`build_plan` 的 `output_shape` 或参照的下标算错）；形状对但元素错说明**搬运**错了（拷贝步长、偏移、屏障时序）。两个失败指纹直接把问题定位到 u3（plan 层）还是 u5（传输层）——这正是 u9-l1 讲的「布局正确性与传输正确性解耦」在失败报告上的体现。

### 4.2 覆盖维度清单与 dtype 全表

#### 4.2.1 概念说明

`correctness.py` 的难点不在「比对」，而在**很多正确性质是结果本身证明不了的**：一个每轮都重新分配窗口的实现照样产出正确结果；一个从不重叠的「async」也产出正确结果。所以 worker 的写法是「结果位精确 + 探针断言」双轨：

- **结果断言**：`check.equal(...)` 逐位对照。
- **探针断言**：`epoch_debug`（窗口有没有被重新分配、零拷贝路径有没有走到）、`staging_slots_debug`（暂存池长没长）、`data_ptr()` 集合（lend 轮转走没走窗口）。

#### 4.2.2 核心流程：维度清单总表

| 维度 | 代码位置 | 断言方式 |
| --- | --- | --- |
| 偶均分 × 双 mode × fp16/bf16 × d∈{64,128,256,512} | L95-L104 | 位精确 vs `reference_even` |
| 不均分（差 1 token 近均 / 极端倾斜）× 双 mode | L106-L126 | 位精确 vs `reference_uneven` |
| `out=` 三态：缺省 / 普通张量 / 窗口本身 | L128-L159 | 位精确 + epoch 探针验路径 |
| 往返：mode 1 ∘ mode 0 = id | L161-L162 | 位精确 |
| async（含 out= 窗口）与 sync 一致 | L164-L183 | 位精确 |
| async 非连续输入 | L167-L177 | 位精确 |
| 5 个同形状 async 并发在飞 | L185-L219 | 位精确 + `staging_slots_debug` 增长 `[1,2,3,4,4]` |
| `lend=True` 轮转 4 窗口 | L221-L245 | 位精确 + `data_ptr` 集合断言 |
| `lend=True` 触发窗口增长（mode 1 更大形状） | L247-L249 | 位精确 |
| 20 轮复用同一窗口不重分配 | L251-L272 | 位精确 + epoch 增量恰为 `2*rounds` |
| dtype 全表：fp32 / fp8e4m3 / fp8e5m2 / int8 / uint8 | L274-L294 | 往返 + 按 `uint8` 字节比较 |
| backward = 另一个 mode（双 mode） | L296-L308 | 位精确 |
| backward 的 splits 原样传递（倾斜切分） | L310-L325 | 位精确（错换表会先挂在形状上） |
| forward AD 携带 tangent | L330-L346 | 位精确（tangent == 再做一次前向） |
| 双重 backward | L348-L358 | 位精确（\(\partial^2 \sum y^2 / \partial x^2 = 2\)） |
| async 在非默认流上发出 | L360-L374 | 位精确（ballast GEMM 使乱序读可见） |
| `empty_output` 返回普通张量 | L376-L387 | 无 grad_fn / 是叶子 |
| Meta 内核形状传播 | L389-L397 | FakeTensor 只出形状不碰设备 |
| timed 变体搬运同一批字节 | L399-L442 | 位精确 + 四阶段数值健全性 |

（行号均指 [test/distributed/correctness.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py)。）

#### 4.2.3 源码精读：三个「结果证明不了」的探针

**dtype 全表为什么按字节比较**（[test/distributed/correctness.py:L274-L294](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L274-L294)）：`all_to_all_single` 不支持 float8，绝对参照在这里断了。L289-L294 的做法是往返后 `back.view(torch.uint8)` 与 `xd.view(torch.uint8)` 比较——「排列的参照不需要知道字节是什么意思」。L276-L279 的注释同时交代了这个判据的盲区：**往返看不到两个 mode 之间对称的寻址错误**，所以一字节元素大小的绝对参照由 `test_plan.py` 的重放承担（它对这些 dtype 参数化，见 u9-l1）。一个维度、两层测试，各管一半。

**20 轮同窗口**（[test/distributed/correctness.py:L251-L272](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L251-L272)）：注释直说「Correct results do not show this: a window reallocated every round would produce them too」。检测器是 epoch——分配新窗口会把 signal pad 清零（u4-l1），每 次调用过两道屏障各 +1，所以 20 轮必须恰 +40（L265 的 `e_after - e_before != 2 * rounds` 即失败）。L259 特意用一个 1 元素的普通张量做探针，让它解析到内部 sync 窗口，且 L256-L258 论证这个窗口已到高水位、不可能中途增长——排除掉另一种 epoch 偏短的假象。

**out= 三态与 epoch 探针**（[test/distributed/correctness.py:L128-L153](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L128-L153)）：L133/L136 分别对照普通张量与 `empty_output` 窗口两条 `out=` 路径。但 L137-L141 的注释点破：方法原样返回传入对象，指针比较恒真；**真正可能悄悄不同的是路径**——没被认成窗口的缓冲会退回内部窗口 + copy-out，结果照样对、不报错。检测器是 L143 的 `epoch_debug(window, 0)`：只有真正跑在这块缓冲上的屏障会把它的 epoch 推到恰好 2（一次调用两道屏障）。分配时 signal pad 被清零，所以初值确定。

#### 4.2.4 代码实践（示例代码，纯 CPU 可跑）

1. **实践目标**：亲眼看到「往返位精确」如何放过一个对称的寻址错误，从而理解 4.2.3 里 dtype 段为什么必须借力 `test_plan.py` 的绝对重放。
2. **操作步骤**：运行下面这段**示例代码**（非项目代码，块级模拟 mode 0/mode 1 的搬运几何）：

```python
import numpy as np

rng = np.random.default_rng(7)
s_total, n_total, d = 6, 6, 2
seq_splits, head_splits = [2, 3, 1], [2, 2, 2]
S = np.cumsum([0] + seq_splits)            # 序列所有权表前缀和 [0,2,5,6]
H = np.cumsum([0] + head_splits)           # 头所有权表前缀和   [0,2,4,6]
ws = 3

X = rng.integers(0, 256, (s_total, n_total, d), dtype=np.uint8)   # 全局张量

def mode0(Htab):
    # rank r 把块 (r,p)=X 的 r 序列段 x p 头段 发给 p，落在 p 窗口行偏移 S[r]
    out = [np.zeros((s_total, h, d), np.uint8) for h in head_splits]
    for r in range(ws):
        for p in range(ws):
            out[p][S[r]:S[r+1]] = X[S[r]:S[r+1], Htab[p]:Htab[p+1]]
    return out

def mode1(windows, Htab):
    # 逆方向：rank r 的窗口行段 S[p]:S[p+1] 发回序列主人 p，按同一张 Htab 归位头列
    back = [np.zeros((s, n_total, d), np.uint8) for s in seq_splits]
    for r in range(ws):
        for p in range(ws):
            back[p][:, Htab[r]:Htab[r+1]] = windows[r][S[p]:S[p+1]]
    return back

shards = np.split(X, S[1:-1], axis=0)      # 每个 rank 的原始序列分片

assert all((c == x).all() for c, x in zip(mode1(mode0(H), H), shards))  # 正确实现：往返还原

H_bad = H.copy(); H_bad[1], H_bad[2] = H[2], H[1]   # 对称错误：1、2 号头段互换（等长才放得下）
w_ok, w_bad = mode0(H), mode0(H_bad)
assert not all((a == b).all() for a, b in zip(w_ok, w_bad))            # 绝对结果：错了
assert all((c == x).all() for c, x in zip(mode1(w_bad, H_bad), shards))  # 往返：却“通过”
print("symmetric addressing error survives the round trip")
```

3. **需要观察的现象**：`w_bad` 与 `w_ok` 在 1、2 号 rank 的输出上不同（绝对错误），但 `mode1(w_bad, H_bad)` 仍逐位还原出原始分片（往返「正确」）。
4. **预期结果**：两个 assert 都通过并打印最后一行——对称错误只有绝对参照能抓住；这正是 correctness.py L276-L279 把一字节 dtype 的绝对判据外包给 `test_plan.py` 重放的原因。
5. 本实践在任何有 numpy 的机器上可复现；若你的 numpy 版本对 `integers(..., dtype=np.uint8)` 行为不同，改用 `rng.integers(0, 200, ...).astype(np.uint8)`。

#### 4.2.5 小练习与答案

**练习 1**：20 轮同窗口的断言为什么用 `2 * rounds` 而不是 `>= 2 * rounds`？
**答案**：epoch 偏小只有一种解释——中途发生了窗口重分配（signal pad 被清零重新计数）或某道屏障被跳过；偏大同样异常（多出别的调用共用了这个窗口）。恰好等于才同时排除两种漂移，这是一个等式断言而不是不等式。

**练习 2**：L199-L218 的 5 个并发 async 里，为什么要有 `torch.cuda._sleep(100_000_000)`，注释还说增长序列出现 `[1, 1, ...]` 是「a blind run and not a passing one」？
**答案**：没有 sleep 时，第 2 次 async 发出前第 1 次可能已经整体完成，暂存槽被 `cudaEventQuery` 判定为空闲而复用——增长序列变成 `[1,1,...]`。这时「池会增长」根本没有被观察到，断言 `growth == [1,2,3,4,4]` 形同虚设。sleep 把 caller 流占住，使 comm 流等它、5 次交换都无法退役，逼出真实的并发路径；第 5 个停在 4 是池的上限（u7-l2），那一次走的是「等 release 事件」而非「查询」的唯一分支。

### 4.3 backward 不交换断言：mode 1 是 mode 0 的逆映射

#### 4.3.1 概念说明

fast-ulysses 的交换是纯数据搬运，算子上看是一个**排列矩阵** \(P\)：\(y = Px\)，\(P\) 只重排元素。它的 vjp 是 \(x_{\text{grad}} = P^{\top} g\)；排列矩阵满足 \(P^{-1} = P^{\top}\)，所以 **backward 就是「用 mode \(1-m\) 再做一次前向」**——搬同样的字节、零重算、天然位精确（u6-l3）。

关键推论是 **splits 表不交换**。`seq_splits`/`head_splits` 描述的是**组的所有权几何**——哪个 rank 拥有哪段序列、哪些头——而不是「本次调用谁发给谁多少」（那是 `all_to_all_single` 的 `split_sizes` 语义，其 backward 确实要交换）。方向翻转只是换了「哪根轴被散射」，所有权表不变；换了反而改动了组几何，在倾斜切分下连形状校验都过不去。`correctness.py` 专门有一条断言防有人「顺手修好」backward 把表换掉。

#### 4.3.2 核心流程：从 build_plan 推出互逆

记 \(S_r\)、\(H_p\) 为两张 splits 表的排他前缀和，\(s_r\)、\(h_p\) 为表项，\(D = d\cdot\text{elem\_size}\) 个字节。元素逻辑坐标写 \((b, \sigma, \eta, j)\)：批、全局序列位置、全局头号、通道。头号 \(\eta\) 属于唯一的 rank \(p(\eta)\)（满足 \(H_p \le \eta < H_p + h_p\)），序列位置 \(\sigma\) 同理属于 \(r(\sigma)\)。

对照 `build_plan` 的发射循环（4.3.3 精读），两个 mode 在坐标上的作用是：

\[
\text{mode 0:}\quad (b,\sigma,\eta,j)\ \text{at}\ r \;\longmapsto\; (b,\ \sigma,\ \eta - H_{p(\eta)},\ j)\ \text{at}\ p(\eta)
\]

\[
\text{mode 1:}\quad (b,\sigma,\eta - H_{r'},\ j)\ \text{at}\ r' \;\longmapsto\; (b,\ \sigma - S_{p(\sigma)},\ \eta,\ j)\ \text{at}\ p(\sigma)
\]

直观读法：mode 0 **序列坐标原样带走、头坐标全局改局部**，元素搬到头的owner；mode 1 **头坐标加回段基址变全局、序列坐标全局改局部**，元素搬到序列的owner。

复合（mode 0 的输出直接喂 mode 1）：从 rank \(r\) 出发的 \((b,\sigma,\eta,j)\) 先到 \(r' = p(\eta)\)；再作用 mode 1 时 \(\sigma\) 的主人仍是 \(r\)（因为 \(\sigma \in [S_r, S_r + s_r)\)），头坐标 \((\eta - H_{r'}) + H_{r'} = \eta\) 复原，落回 \((b, \sigma - S_r, \eta, j)\) at \(r\)——正是原始元素。于是：

\[
\text{mode 1} \circ \text{mode 0} = \mathrm{id}
\qquad\Longrightarrow\qquad
\text{mode 1} = (\text{mode 0})^{-1} = P^{\top}
\]

整条推导里两张表只以 \(S_\cdot\)、\(H_\cdot\) 的身份出现，**两个 mode 用的是同一组表**；它们是所有权表，不是流量表（mode 0 从 \(r\) 发往 \(p\) 的字节数 \(s_r h_p D\) 是从所有权推导出来的）。

#### 4.3.3 源码精读

**发射循环的镜像结构**（[src/a2a_plan.cc:L175-L213](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L175-L213)）：

- mode 0 分支（L176-L192）：`src_offset = head_offset[p] * d_bytes`、`src_pitch = head_total * d_bytes`——沿头轴以满行宽跨步**读**；`dst_offset = seq_offset[rank] * row`、`dst_pitch = row`——在 peer 窗口里从本 rank 序列段起点开始**连续写**。这正是坐标式里「\(\eta\) 减 \(H_p\)、\(\sigma\) 不动、搬到 \(p(\eta)\)」的字节化。
- mode 1 分支（L194-L212）：`src_offset = seq_offset[p] * row`、`src_pitch = row`——沿序列轴**连续读** peer 拥有的行；`dst_offset = head_offset[rank] * d_bytes`、`dst_pitch = head_total * d_bytes`——在 peer 窗口里按头步长**跨步写**自己头列的位置。L195-L198 的注释说明为什么必须跨步写：窗口即返回张量，没有本地交织的第二次机会。这是「\(\sigma\) 减 \(S_p\)、\(\eta\) 加回 \(H_{r'}\)、搬到 \(p(\sigma)\)」。
- `output_shape`（[src/a2a_plan.cc:L162-L164](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L162-L164)）：mode 0 给 \((b, \text{seq\_total}, h_{\text{me}}, d)\)、mode 1 给 \((b, s_{\text{me}}, \text{head\_total}, d)\)——两式互换 \(s\) 与 \(h\) 的角色，正是互逆的形状表达。

**backward 的位精确断言**（[test/distributed/correctness.py:L296-L308](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L296-L308)）：L303 对 `mode` 各做一次 `backward(upstream)`，L304-L307 断言 `xg.grad` 位精确等于 `group.all_to_all_4d(upstream, mode=1 - mode)`——把「backward 是另一个 mode」当成可执行命题直接验证。L297-L298 的注释给出理由：backward 搬的是同样的字节、不重算任何东西，「close」会藏住一个错误的 plan。

**splits 原样传递的断言**（[test/distributed/correctness.py:L310-L325](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L310-L325)）：这是本讲实践要追的靶子。L313 特意选**极端倾斜**的 `seq_splits = [s_total-(ws-1)] + [1]*(ws-1)`；L320 前向带 `**kw` 两张表，backward 之后 L321-L325 断言梯度位精确等于 `group.all_to_all_4d(up, mode=1, **kw)`——**同一对 kw，原样传给 mode 1**。L310-L312 的注释说破意图：这条断言专抓「按 all_to_all_single 的习惯把 backward 的 splits 换掉」的「修复」，而且换了表在倾斜切分下**先挂在形状上**，不只是值错——L319 的 `up` 是按换表前的输出形状造的，形状失配立即显形。

#### 4.3.4 代码实践（无 GPU，仅需构建好的 `_C` 扩展）

1. **实践目标**：把 4.3.2 的坐标推导落成可运行的证据，并用「换表」亲手触发它预言的失败。
2. **操作步骤**：在仓库根目录写一个小脚本（示例代码），复用 [test/test_plan.py:L27-L37](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L27-L37) 的 `build_plan` 与 [L40-L69](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L40-L69) 的 `apply_ops`/`run_plan` 思路（可直接 `sys.path.insert(0, "test")` 后 `from test_plan import build_plan, apply_ops`）：

```python
import numpy as np
from test_plan import build_plan, apply_ops   # a2a_plan_debug 的薄封装，纯主机、不需要 GPU

ws, b, d, es = 4, 1, 8, 2
seq_splits, head_splits = [5, 3, 0, 7], [2, 2, 1, 1]   # 含 0 长分片的倾斜切分
S = np.cumsum([0] + seq_splits); H = np.cumsum([0] + head_splits)

plans0 = [build_plan(b, d, r, ws, seq_splits, head_splits, 0, es) for r in range(ws)]
plans1 = [build_plan(b, d, r, ws, seq_splits, head_splits, 1, es) for r in range(ws)]

# 全局字节张量按所有权切成各 rank 的 mode 0 输入（b, s_r, head_total, d)
X = np.random.default_rng(0).integers(0, 256, (b, S[-1], H[-1], d), dtype=np.uint8)
inputs = [np.ascontiguousarray(X[:, S[r]:S[r+1]]) for r in range(ws)]

# 第一跳：重放 mode 0 的 CopyOp，rank r 写进 peer p 的窗口
win0 = [np.zeros(p["output_bytes"], np.uint8) for p in plans0]
for r, plan in enumerate(plans0):
    for op in plan["ops"]:
        apply_ops([op], inputs[r].reshape(-1), win0[op[0]])

# 第二跳：以第一跳的窗口为源重放 mode 1（其源布局是“满序列 x 自己的头”）
win1 = [np.zeros(p["output_bytes"], np.uint8) for p in plans1]
for r, plan in enumerate(plans1):
    for op in plan["ops"]:
        apply_ops([op], win0[r], win1[op[0]])

# mode 1 的输出形状 == mode 0 的输入形状：(b, s_r, head_total, d)
print("round trip restores:", all(
    (win1[r][: p["output_bytes"]] == inputs[r].reshape(-1)).all()
    for r, p in enumerate(plans1)))
```

   写脚本时以 `test_plan.py` 的 [test_round_trip](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L236)（L236 起）为模板——它做的就是这件事的权威版本；上面草稿里第二跳的窗口→输入对位以那个测试为准（示例代码，未在本机运行，**待本地验证**）。
3. **加一步「换表」实验**：把第二跳的 plan 改成 `build_plan(b, d, r, ws, head_splits, seq_splits, 1, es)`（把两张表像 `all_to_all_single` 的 backward 那样**交换**传入）。注意交换后 `output_shape` 变成 \((b, h_r, \text{seq\_total}, d)\)，与本 rank 输入的形状不再相同——还原检查先在**形状**上就不成立，即便改写比较代码硬比字节，偏移也是全错的。
4. **需要观察的现象**：原样传表 → 逐字节还原；交换传表 → 偏移全错、还原失败；而在真实算子路径里这甚至轮不到搬字节——`make_dims` 的形状交叉校验（[src/group.cc:L101](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L101) 的「`but the splits imply`」）会先抛错，因为输入形状与换表后推出的形状对不上。host-only 重放里没有 `make_dims`（u9-l1 讲过：`a2a_plan_debug` 绕过它），所以错误以「搬错字节」而非「raise」显形——这恰好演示了 4.4 的主题：**校验在 torch 层，寻址在 plan 层**。
5. **预期结果**：`True` / `False` 各一次。请在实践报告里写出 4.3.2 的推导作为理论依据（哪个 offset 对应坐标式中的哪一项）。

#### 4.3.5 小练习与答案

**练习 1**：为什么倾斜切分（而不是均分）是「splits 不交换」断言的有效载体？
**答案**：均分下交换两张表结果不变（对称情形），断言测不出任何东西；倾斜切分下两张表不对称，换表后 `make_dims` 推出的形状与实际输入不同，错误从「值错」升级为「形状错」，要么 raise、要么重放中偏移可见地错位。测试选最不利的输入，而不是最顺手的。

**练习 2**：mode 0 从 rank \(r\) 发给 peer \(p\) 的字节量是多少？据此说明流量表与所有权表的区别。
**答案**：\(s_r \cdot h_p \cdot D\) 字节（`rows = s_me`、`width = head_splits[p]*d_bytes`）。它是**两张表各取一项的乘积**，随 \((r,p)\) 变化；而所有权表每项只描述一个 rank 的持有量。`all_to_all_single` 的 `split_sizes` 直接以流量为参数，方向翻转时收发角色互换所以必须交换；fast-ulysses 的 splits 是所有权，流量是导出量，方向翻转不改变所有权。

### 4.4 拒绝消息断言：先于第一个握手

#### 4.4.1 概念说明

`validation.py` 测的是另一半世界：**算子拒绝它声明会拒绝的东西吗？拒绝得够早吗？**（[test/distributed/validation.py:L1-L33](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/validation.py#L1-L33)）。它的存在理由写在 docstring L5-L7：`test_plan.py` 从 `a2a_plan_debug` 进去、手拼 `A2ADims`，从不经过 `make_dims` 或 `prepare`；`correctness.py` 只传合法参数——**docs/api.md 的 Raises 清单此前没有任何测试覆盖**。

两个设计决定值得先记住：

1. **消息即契约**：断言不看异常类型（类型是 torch 的），只看 `str(exc)` 里含不含指定的 fragment——这些 fragment 与 [docs/api.md:L93-L103](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md#L93-L103) 的 Raises 一节及源码 `TORCH_CHECK` 的措辞一一对应。改消息措辞会挂测试，文档、源码、测试三方被钉在一起。
2. **挂起即失败模式**：docstring L9-L11 说，每个拒绝在**每个 rank**上检查、之后全组碰 barrier。一个只在部分 rank 抛出的检查、或抛在调用第一个握手**之后**的检查，会把这个 worker **挂死**而不是打出 FAIL——「which is the failure mode worth catching」。这就是「验证先于第一个屏障」被检验的方式：它是一个负向性质，快速通过本身就是证明，挂起就是违规的 localize。

#### 4.4.2 核心流程

`Rejects.raises(name, fragment, fn)` 三步：

```text
1. 执行 fn，必须抛异常；没抛 → FAIL "accepted"
2. 消息必须含 fragment；不含 → FAIL "raised, but not for the stated reason"
3. rank 0 打 OK；然后 dist.barrier()   ← 全组对齐：非一致或过晚的拒绝会死在这里
```

`accepts(name, fn)` 是配对的反向探针：断言**不**抛——用于验证一次拒绝没有留下脏状态（暂存池、计划缓存、lend 槽位），以及某些「合法边界」确实合法。

#### 4.4.3 源码精读

**裁判本体**（[test/distributed/validation.py:L45-L76](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/validation.py#L45-L76)）：L53 的 `except Exception` 配注释「the type is torch's, the message is the contract」；L62-L64 的注释点明 barrier 的意义——如果拒绝不是 rank 一致的、或发生在开路握手之后，ranks 已失步，死在这里的就是本 worker。

**与源码逐条对应的消息**（示例三条，全表同理）：

- `mode 2` → fragment `"mode must be 0 or 1"`（[test/distributed/validation.py:L109](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/validation.py#L109)），消息源是 [src/group.cc:L72](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L72) 的 `TORCH_CHECK(mode == 0 || mode == 1, "mode must be 0 or 1, got ", mode)`。注意 [src/a2a_plan.cc:L150](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L150) 还有一份带括号展开的同类检查——公开 API 走 `make_dims` 先被拦，`build_plan` 里那份是纵深防御。
- `splits that contradict the shape` → fragment `"but the splits imply"`（[test/distributed/validation.py:L118-L121](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/validation.py#L118-L121)），消息源是 [src/group.cc:L101](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L101)（`make_dims` 的交叉校验，u2-l3 讲过）。
- `a fifth live lent result` → fragment `"lent results may be alive at once"`（[test/distributed/validation.py:L331-L336](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/validation.py#L331-L336)），消息源是 [src/group.cc:L293](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L293)；L325-L330 的注释复述了 u4-l4 的论证：拒绝而非软化，因为软化决策只基于本 rank 的引用计数，会让各 rank 选中不同窗口而挂死。

**「状态先行」的用例设计**：[test/distributed/validation.py:L269-L278](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/validation.py#L269-L278) 先做一次偶均分调用**预热计划缓存**，再传空 splits 列表断言 `"seq_splits has 0 entries"`——注释说明：曾以裸向量为键时两种调用键相同、第二次继承了第一次的 plan 而非自己的拒绝；不先预热，这条断言什么都没测。[L312-L324](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/validation.py#L312-L324) 的 inference_mode 段同构：第一次调用只是布陷阱（在 inference mode 里分配的暂存缓冲会变成 inference tensor），**第二次** accepts 才是检查。[L280-L310](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/validation.py#L280-L310) 的错设备段还配了 L301-L303 的 accepts：拒绝之后同形状必须仍然可用（暂存池没被毒化）。

**两处「故意不覆盖」的守卫**（[test/distributed/validation.py:L19-L32](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/validation.py#L19-L32)）：`prepare()` 里比较窗口字节容量与本次需求的两个守卫，从公开 API **不可达**——小缓冲先挂在形状检查；异 dtype 视图需要 storage 可扩而 torch 拒绝构造，除非窗口严格大于本 rank 输出（仅不均分时成立），而那会造出 rank 相关的 `out`、让 `window()` 是否分配在 rank 间分歧——那是集体操作，worker 会挂而非报。docstring 把「为什么不可达」写下来，防止后人误以为没测是疏漏。同理 L13-L17 说明重叠检查按**容量**而非按本次需求比较：唯一可达的情形已被豁免，把守卫改成需求形式反而无人能测，「leave the capacity form alone」。

**收尾**（[test/distributed/validation.py:L385-L391](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/validation.py#L385-L391)）：与 correctness 相同的 all_reduce 裁决 + 非零退出码。

#### 4.4.4 代码实践（纸面设计 + 可选本地验证）

1. **实践目标**：为 `validation.py` 设计两个新的非法用例，写出 fragment 与预期消息，并预判它们各自在哪个源码检查点、以什么时序失败。
2. **操作步骤**：
   - **用例 A：`mode=3`**。在 [validation.py:L109](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/validation.py#L109) 的 mode 2 检查后追加一行同构代码：

     ```python
     check.raises("mode 3", "mode must be 0 or 1", lambda: group.all_to_all_4d(good, mode=3))
     ```

     预期消息：`mode must be 0 or 1, got 3`，来自 [src/group.cc:L72](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L72)（`make_dims`，经 `plan()` 在 `prepare()` 校验链中先行），全 rank 一致、先于任何握手；预期输出 `OK ws=<N> rejects mode 3`。
   - **用例 B：world_size 超界**。这不是调用级拒绝而是**构造级**拒绝——[docs/api.md:L103](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md#L103) 明说 `world_size outside [1, 8] is refused by the constructor, so no call reaches it`。写出你预期的检查位置与消息：[src/group.cc:L145-L155](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L145-L155) 的 `UlyssesGroup` 构造函数，`world_size must be in [1, 8] -- fast-ulysses is single-node -- got 9`。要实际触发需 `torchrun --nproc_per_node=9`（两个 rank 共享一块 GPU 即可），且 C++ 构造检查发生在任何集合 bootstrap 之前、各 rank 本地独立抛出——所以是干净失败而非挂起。
3. **需要观察的现象**：用例 A 在本地插入后跑 `torchrun --nproc_per_node=<N> test/distributed/validation.py`，应在 mode 2 的 OK 行后多出一行 `OK ... rejects mode 3`，且总耗时几乎不变（没有集合操作被多付）。
4. **预期结果**：A 的消息与 fragment 完全吻合；B 在纸面推演中给出「构造期、全 rank 一致、先于一切通信」的结论。B 的实际运行（9 进程）**待本地验证**。
5. **追问（可选）**：`mode=-1` 会走到哪条消息？`seq_splits` 里出现负数呢？——先在 [src/group.cc](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc) 与 [src/a2a_plan.cc:L133-L140](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L133-L140) 里找答案再验证：负数张量维度在 torch 侧就无法构造，`a2a_plan` 的负数检查同样是纵深防御而非可达路径。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `Rejects.raises` 里的 `dist.barrier()`（L64）能检验「拒绝先于第一个握手」？它是如何把「过晚的检查」变成可观测信号的？
**答案**：若检查抛在调用的开路屏障之后，抛错的 rank 已让其余 rank 进入 GPU 自旋等待；随后的 `dist.barrier()` 需要 CPU 侧全员到达，而被卡的 rank 永远到不了——worker 挂起。快速、全组一致地通过每一个 barrier，就是「所有拒绝都发生在任何握手之前」的运行时证明；挂起本身就是违规的定位信号（配合 u9-l1 的超时三段式清理回收进程）。

**练习 2**：`check.accepts`（L66-L76）在文件里出现多次，删掉它们文件会更短。留着它们的理由是什么？
**答案**：它们验证「一次拒绝没有留下脏状态」。错设备段用它确认暂存池没被毒化（L301-L303），inference_mode 段用它确认第二次同形状调用仍然可用（L321-L324），lend 段用它确认丢弃旧结果后第 5 次调用恢复合法（L338-L341）。只测拒绝不测善后，会漏掉「拒绝本身引入的新 bug」这一类回归。

## 5. 综合实践

把本讲三条线串成一个任务：**给这两个 worker 写一份「覆盖审计」**。

1. **运行并收集**（需 ≥2 GPU；无多卡环境时改做第 2、3 步并标注「待本地验证」）：

   ```bash
   torchrun --nproc_per_node=8 test/distributed/correctness.py
   torchrun --nproc_per_node=8 test/distributed/validation.py
   # 或走启动壳（自动含 3 卡的非 2 幂世界）：
   pytest -m multigpu -k "correctness or validation" test/test_distributed.py
   ```

   把两份输出的每个 `OK ws=...` 行收集进 4.2.2 的维度总表，勾出每个维度实际被哪条 OK 覆盖；找出「表里有、输出里没有」的缺口并解释（提示：`ws > 1` 守卫的条目在单卡运行时会静默跳过，见 [correctness.py:L142](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L142)、[L265](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L265)——`ws == 1` 时屏障内核根本不发射，epoch 不会前进，u5-l3 讲过）。
2. **无 GPU 部分**：完成 4.3.4 的 host-only 重放（原样传表 → 往返还原；交换传表 → 失败），并在报告里附上 4.3.2 的坐标推导，标明 `src_offset/dst_offset/pitch/width` 各对应坐标式中的哪一项。
3. **设计部分**：完成 4.4.4 的两个新非法用例（`mode=3`、`world_size` 超界），写出插入位置、fragment、预期消息与失败时序（先于/后于第一个握手），有条件时插入你自己的工作副本验证。
4. **验收标准**：审计表无未解释的缺口；重放实验两个断言结果与预言一致；新用例的预期消息与源码 `TORCH_CHECK` 逐字对上。

## 6. 本讲小结

- `correctness.py` 的铁律是**位精确**：纯数据搬运没有浮点归约，「close enough」不是通过而是被容差藏住的 bug；参照选用与实现零共享、且被 sglang 现实使用的 `all_to_all_single + permute`（偶均分）与列表版 `dist.all_to_all`（不均分）。
- 覆盖是**结果断言 + 探针断言**双轨：epoch 探针证明零拷贝路径被走到、窗口 20 轮未被重分配；`staging_slots_debug` 证明 async 真并发（`[1,2,3,4,4]`）；`data_ptr` 集合证明 lend 轮转走了 4 个窗口。结果证明不了的性质，全靠探针。
- dtype 全表分两层：fp16/bf16 有绝对参照；一字节 dtype 因 `all_to_all_single` 不支持 float8 而退为**按字节比较的往返**，其盲区（对称寻址错误）由 `test_plan.py` 的绝对重放补上——一个维度、两层测试。
- backward 是 \(P^{\top}\)，即 mode \(1-m\) 的前向：从 `build_plan` 的坐标作用可推出 \(\text{mode 1} \circ \text{mode 0} = \mathrm{id}\)；splits 是**所有权表**而非流量表，backward 原样传递，倾斜切分下「换表修复」会先挂在形状上——correctness.py 有专门断言看守这一点。
- `validation.py` 把 docs/api.md 的 Raises 清单变成可执行契约：消息 fragment 钉住文档、源码、测试三方；每个检查全 rank 一致执行后碰 barrier——**挂起而非 FAIL** 就是「拒绝晚于第一个握手」或「拒绝不一致」的暴露方式，这正是「校验先于第一个屏障」承诺的运行时检验。
- 好的拒绝测试还要测**善后**：`accepts` 探针确认拒绝没有毒化暂存池、计划缓存与 lend 槽位；docstring 会为「故意不覆盖的守卫」写下不可达的原因，防止后人误判为疏漏。

## 7. 下一步学习建议

下一讲 **u9-l3 对抗性测试**进入更狠的一层：`ce_ordering.py`、`window_race.py`、`overlapping_barriers.py` 与 `cudagraph.py` 不再问「对不对」，而是问「测试自己还能失败吗」——负控制（armed 必须让测试读到陈旧字节）、传输内 skew、单缓冲窗口的读写竞态。建议带着本讲的两个问题去读：correctness 的位精确为什么抓不住**时序**错误（它每步都有 barrier 兜底）？validation 的「先于握手」为什么证明不了**屏障之间**的顺序？另外可提前重读 [src/barrier.cu](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/barrier.cu)（u5-l3）与 [test/distributed/cudagraph.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/cudagraph.py)，把「epoch 每次重放恰 +2」的断言与本讲的 epoch 探针对照着看。
