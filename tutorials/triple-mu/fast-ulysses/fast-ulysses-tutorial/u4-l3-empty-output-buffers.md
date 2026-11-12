# u4-l3 empty_output 与调用者持有的缓冲

## 1. 本讲目标

学完本讲，你应该能够：

- 解释 `empty_output()` 返回的到底是什么：一次**集合性的对称内存分配**中「最大窗口」的密集前缀视图，以及它为什么比看起来大。
- 说出 `owned_` 按地址记账、`prune_owned()` 按 storage 引用计数回收的生命周期模型，并解释为什么 `empty_output()` 的缓冲可以比 group 活得更久。
- 区分 `out=` 的三条路径：对称缓冲零拷贝、普通张量拷入、默认分配，并指出零拷贝路径上 `copy_out` 为什么整个消失。
- 复述「`out=` 不可微、且 raise 而非静默丢梯度」这条约定的完整理由链。

本讲承接 u4-l1（对称内存窗口的分配）与 u4-l2（窗口按 `(role, dtype)` 缓存与「触发分配的量必须全 rank 相同」的约束），并把 u3-l2 建立的「窗口密集前缀即完整输出」不变量落到调用者的手里。

## 2. 前置知识

本讲只需要三个 torch 的基础概念，用通俗语言各说一遍：

- **视图（view）与 `narrow`**：一个张量可以不复制数据、只换一种「看同一块内存的方式」。`t.narrow(0, 0, n)` 取第 0 维前 `n` 个元素，`t.view(shape)` 重排形状。视图与原张量共享同一份 **storage**（底层存储），改一个另一个也变。
- **storage 引用计数**：每份存储记着「有几个张量对象在引用我」。所有引用都消失，存储才释放。这是本讲生命周期模型的核心机制。
- **单缓冲窗口**：u4-l1/u4-l2 建立的概念——每个窗口同一时刻只装一次调用的结果，下一次调用会直接覆盖它。所以「一个缓冲一次只能喂一个调用」。

另外回顾两个前讲结论，本讲反复用到：

- u3-l2：**本 rank 自己的份额也经过窗口**，所以「窗口的密集前缀 = 本 rank 的完整输出」，`copy_out` 因此只是一条平坦的设备内拷贝。
- u4-l1：`allocate()` 内部的 `empty_strided_p2p` + `rendezvous` 都是**集合操作**，且 rendezvous 按到达顺序配对——所以「触发分配的量必须全 rank 相同」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/group.cc](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc) | 有状态层：`make_output`、`prune_owned`、`window_of`、`allocate`、`destroy` |
| [src/bindings.cc](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc) | torch 算子层：`empty_output` 算子、`prepare()` 的 `out=` 三态判定、`copy_out`、autograd 拒绝 |
| [include/fast_ulysses/group.hpp](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/group.hpp) | 契约注释与成员声明：`Window`、`owned_`、`make_output`/`window_of` |
| [python/fast_ulysses/group.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py) | Python 门面：`all_to_all_4d` 的 `out=` 分支与拒绝、`empty_output`、`_timed` |
| [src/a2a_plan.cc](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc) | 纯主机寻址层：`window_numel` 取全组最大的循环（u3 已精读，本讲引用结论） |
| [docs/api.md](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md) | 契约文档：`out=` 三态、不可微约定、`empty_output` 一节 |

## 4. 核心概念与源码讲解

### 4.1 make_output 视图：分配最大窗口，返回密集前缀

#### 4.1.1 概念说明

`empty_output(x, mode=0)` 的作用是：**替调用者预先分配好这次调用的输出缓冲，而且放在对称内存里**。为什么值得单独做一个 API？

- 普通调用（不传 `out=`）的流程是「peers 把数据写进 group 内部窗口 → 一条设备内拷贝搬到调用者的张量」。这条搬出拷贝（copy_out）是**同设备拷贝**：它要占内存系统带宽、和你的计算竞争——u1-l1 提过，「端到端零 SM」必须靠 `out=` 去掉它才成立。
- 如果输出缓冲本身就是对称内存，peers 就可以直接把数据写进**调用者手里这个张量**，搬出步骤整个消失。这就是「零拷贝路径」。

但有个大小问题：窗口必须按 `window_numel`（全组最大接收量）分配，而本 rank 的输出可能小于它（不均匀分片时）。所以 `make_output` 分配的是最大窗口，**返回给调用者的只是它的密集前缀视图**。这正是文档那句「It is larger than it looks」的含义：

> It is larger than it looks. Under uneven splits the window has to hold the **largest** rank's output, because the peer offsets only line up while every rank allocates the same size; the tensor you get is a view of that allocation's dense prefix.
> —— [docs/api.md:159-161](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md#L159-L161)

#### 4.1.2 核心流程

`window_numel` 的取法（u3-l1/u3-l2 已精读，这里只取结论）：plan 层对每个 peer 计算其接收元素数，取最大值——

- mode 0（scatter head）：peer p 最终持有「全序列 × 自己的头分片」，故

  \[ W \;=\; \text{window\_numel} \;=\; b \cdot s_{\text{total}} \cdot \max_p h_p \cdot d \]

- mode 1（gather head）：镜像，

  \[ W \;=\; b \cdot \max_p s_p \cdot n_{\text{total}} \cdot d \]

而本 rank r 的输出元素数在 mode 0 下是 \( b \cdot s_{\text{total}} \cdot h_r \cdot d \le W \)。

`make_output` 的执行流程（伪代码）：

```text
make_output(shape, dtype, numel = plan.window_numel):
    check_alive()
    prune_owned()                    # 先回收调用者已释放的旧缓冲，腾出内存
    win = allocate(numel, dtype)     # 集合操作：分配最大窗口 + rendezvous + 清零握手区
    out = win.tensor.narrow(0, 0, prod(shape)).view(shape)   # 密集前缀视图
    owned_[win.tensor.data_ptr()] = win   # 按基址记账，供 out= 反查
    return out
```

窗口必须装下最大 rank 输出，原因有三条，每条都能在前讲找到根据：

1. **peer 偏移对齐**：发送方按「接收方窗口布局」计算目的偏移与步长，只有所有 rank 窗口同尺寸，这些偏移才在发送方与接收方之间一致（u3-l2 的窗口断言）。
2. **rendezvous 配对**：`allocate` 是集合操作，若各 rank 的 `numel` 不同，谁分配谁不分配就取决于本地形状，rendezvous 顺序错乱即挂起（u4-l1）。
3. **免通信的同答案**：每 rank 持有完整 splits 表，\(\max_p\) 是全 rank 相同的量——这是 u4-l2「触发分配的量必须全 rank 相同」约束的又一次落地。

#### 4.1.3 源码精读

torch 算子层的入口很短：取 plan，把 `output_shape`、dtype、`window_numel` 交给 `make_output`——

[bindings.cc:609-619](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L609-L619)：`empty_output` 算子先 `require_cuda` 与设备守卫，再由 plan 拿到三样东西——输出形状、dtype、`window_numel`——转手给 `group->make_output`。注意它复用 `plan()` 缓存，所以与真正的调用走**同一份形状推导**，不会出现「分配的缓冲和调用的输出形状对不上」这种第二份规则。

真正的分配在 group 层：

[group.cc:304-312](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L304-L312)：`make_output` 依次做四件事——`prune_owned()`（分配前回收，让刚释放的对称段能被这次分配复用）；`allocate(numel, dtype)`（u4-l1 精读过的集合分配：`empty_strided_p2p` + `rendezvous` + 清零自身握手区 + `sym->barrier`）；`win.tensor.narrow(0, 0, multiply_integers(shape)).view(shape)`（从窗口头部切出本 rank 输出那么多个元素，再 view 成输出形状——**这就是密集前缀视图**）；最后把整个 `Window`（含 `peer_ptrs`/`flag_ptrs`）按 `data_ptr` 存进 `owned_`。

`window_numel` 为什么是这个数，源头在 plan 层：

[a2a_plan.cc:166-173](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L166-L173)：对每个 peer p 计算 `peer_numel`（mode 0 用 `seq_total × head_splits[p]`，mode 1 镜像），`plan.window_numel = std::max(...)` 取最大。`make_output` 的 `numel` 参数就是这个值。

Python 侧只是转发：

[group.py:234-249](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L234-L249)：`empty_output` 的 docstring 点出三件事——传回 `out=` 可去掉 copy-out；缓冲归你所有、可跨步持有、可比 group 长命；**COLLECTIVE，所以在循环外分配**，且一个缓冲同时只服务一个并发调用。

#### 4.1.4 代码实践

**实践目标**：亲手算一次不均匀分片下的 `window_numel` 与前缀视图，验证「窗口比输出大」不是一句空话。

**操作步骤**（源码阅读 + 纸面计算，无需 GPU）：

1. 设定 ws=4、b=1、d=8、fp16（`d × elem_size = 16` 字节，满足对齐），`seq_splits=[5,3,0,7]`（\(s_{\text{total}}=15\)），`head_splits=[13,9,10,8]`（\(n_{\text{total}}=40\)），mode 0。
2. 按上面的公式计算 \( W = 1 \times 15 \times \max(13,9,10,8) \times 8 = 1560 \) 个元素。
3. 计算 rank 3 的输出元素数：\( 1 \times 15 \times 8 \times 8 = 960 \)。
4. 结论：rank 3 的 `empty_output` 缓冲背后是一份 1560 元素的对称分配，拿到手的视图只有前 960 个元素；而 rank 0（\(h_0=13\)）的视图恰好占满窗口。
5. 若有 GPU 环境（≥1 卡），用 `torch.ops.fast_ulysses.a2a_plan_debug`（u3-l3 讲过）打印该形状的 plan，与手算的 `window_numel` 比对；再用 `buf.untyped_storage().nbytes() // buf.element_size()` 观察「视图背后的分配」比你从 `buf.numel()` 看到的大多少。

**需要观察的现象**：视图元素数（960）小于存储元素数（1560）；`buf.data_ptr()` 等于存储的基址（下一模块会解释为什么这点至关重要）。

**预期结果**：手算与 `a2a_plan_debug` 一致。无 GPU 时步骤 1–4 为纯纸面推导，步骤 5 标注**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：ws=1 时还能观察到「窗口大于输出」吗？为什么？

**答案**：不能。ws=1 时唯一的 peer 就是自己，\( W = b \cdot s_{\text{total}} \cdot h_0 \cdot d \) 恰等于本 rank 输出，前缀视图占满整个分配。这也是单卡实验里看不到「多出来的尾巴」的原因——必须靠不均匀多卡分片（或纸面计算）才能看到。

**练习 2**：如果 `make_output` 按本 rank 输出尺寸（而不是 `window_numel`）分配，最先在哪里出问题？

**答案**：在 `allocate()` 的 rendezvous。各 rank 的 `numel` 由各自的 \(h_r\)（mode 0）决定，互不相同，于是「谁调用了分配、分配多大」变成各 rank 的本地决定；rendezvous 按到达顺序配对，大小不一致的分配序列会让配对错乱，结果是挂起而非报错（u4-l1 的顺序论证）。即便 rendezvous 碰巧完成，peer 的目的偏移也是按同尺寸窗口推导的，写入会越过小窗口的边界。

### 4.2 owned_ 与 prune_owned：按地址记账、按引用计数回收

#### 4.2.1 概念说明

`make_output` 把 `Window` 交出去之后，group 面临一个别的模块没有的问题：**这个缓冲的生死由调用者决定**。调用者可能立刻丢掉它、可能持有它跑完整个训练、甚至可能比 group 本身活得久。而 `Window` 里存着 `peer_ptrs`/`flag_ptrs`——传输与屏障寻址全靠它们——group 必须在「调用者还持有缓冲」期间保留这份记录。

解法分两半：

- **记账**：`owned_` 是一个 `std::map<const void*, Window>`，键是窗口基址。`out=` 进来时用 `window_of(out)` 按 `out.data_ptr()` 反查——查得到，说明这个 out 就是我们的窗口。
- **回收**：`prune_owned()` 遍历 `owned_`，凡是 `tensor.storage().use_count() == 1` 的条目就地删除。含义是：storage 只剩 `owned_` 里那份 `Window::tensor` 自己在引用，说明调用者的视图早已释放，对称分配可以归还给分配器。

#### 4.2.2 核心流程

一次缓冲的完整生命周期（文字时序）：

```text
t0  buf = group.empty_output(x, mode=0)
      owned_[base] = Window{...}
      storage 引用计数 = 2（Window::tensor + 返回给调用者的视图）
t1  调用者使用 buf：all_to_all_4d(x, mode=0, out=buf)
      prepare() 里 window_of(buf) 按 base 命中 → 零拷贝路径
t2  调用者 del buf（或名字离开作用域）
      storage 引用计数 = 1（只剩 owned_ 里的 Window::tensor）
t3  下一次任意调用的 prepare()，或下一次 make_output() 的开头
      prune_owned() 发现 use_count == 1 → erase → storage 释放 → 对称段归还
t4  destroy()（若调用者一直持有 buf，则不会发生 t3）
      destroy() 末尾同样 prune_owned()——只归还没人保留的，buf 依旧活着
```

关键不变量：**`use_count == 1` 当且仅当「世上只剩我们自己的记录」**。调用者手里的任何视图（哪怕 `buf[0]` 这样的切片）都会让计数 ≥ 2，缓冲就绝不回收。

`prune_owned()` 有三个调用点，且都遵守同一条纪律——**绝不在还有 `Window&` 活着时调用**（erase 会让指向 map 节点的引用失效）：

| 调用点 | 时机 | 作用 |
| --- | --- | --- |
| `make_output` 开头 | 分配新缓冲前 | 刚释放的段能被本次分配立即复用 |
| `prepare()` 早期 | 取任何 `Window&` 之前 | 只取一个 buffer 又丢掉的调用者，不会把缓冲钉到 `destroy()` |
| `destroy()` 末尾 | 收尾清理 | 只归还无人持有的，其余靠自身 storage 引用继续活 |

#### 4.2.3 源码精读

[group.cc:198-205](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L198-L205)：`prune_owned` 全文——遍历 `owned_`，`storage().use_count() == 1` 即 erase。注释点明依据：调用者的缓冲是窗口 storage 的视图，计数为一说明只剩我们这个持有者，分配可以还给对称分配器。

[group.cc:314-318](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L314-L318)：`window_of`——拿 `out.data_ptr()` 到 `owned_` 里查，查不到返回 `nullptr`（普通张量）。**为什么按地址查找是可行的**：`make_output` 的视图是 `narrow(0, 0, n)`，从偏移 0 开始切，`view` 又不改偏移，所以返回给调用者的张量的 `data_ptr()` 就是窗口基址——记账键与查找键天然相等。

[bindings.cc:99-106](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L99-L106)：`prepare()` 里的调用点，注释说明为什么在这里（而不只在 `make_output`）剪枝：`make_output` 是唯一另一个能剪枝的地方，若调用者取一个缓冲又丢掉，不在这里剪就会把它钉到 `destroy()`。注意位置在一切校验和窗口获取**之前**，且本函数内此后不再调用它——正是上面那条「不使引用失效」纪律的体现。

[group.cc:473-481](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L473-L481)：`destroy()` 的收尾——清空 staging、plans、windows、lent 之后调用 `prune_owned()`。注释与 API 文档一致：调用者还持有的缓冲靠**自身 storage 引用**存活，drop 掉我们的记录只释放无人保留的那些。

[group.hpp:177](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/group.hpp#L177)：`owned_` 的声明——`std::map<const void*, Window>`，注释「handed to callers, by address」。旁边 [group.hpp:67-70](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/group.hpp#L67-L70) 是 `prune_owned` 的契约注释：不得在有活跃 `Window&` 时调用。

[group.cc:310](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L310)：`owned_.emplace(win.tensor.data_ptr(), std::move(win))`——记账动作本身。

#### 4.2.4 代码实践

**实践目标**：源码阅读型实践——追踪 `prune_owned` 的全部调用点，验证它们都满足「无活跃引用」纪律，并写出 `empty_output` 缓冲的生命周期叙述。

**操作步骤**：

1. 在仓库里搜索 `prune_owned`，确认调用点只有 `make_output`（[group.cc:307](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L307)）、`prepare`（[bindings.cc:106](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L106)）、`destroy`（[group.cc:481](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L481)）三处。
2. 对每一处回答：调用之后，本函数还有没有从 `owned_` 取出的 `Window&` 会被使用？（`make_output`：prune 在取 `Window` 之前；`prepare`：prune 在最开头，其后才 `window_of`；`destroy`：末尾，此后不再使用。）
3. 写一段注释版伪代码，描述 4.2.2 的 t0–t4 时序。

**需要观察的现象**：三个调用点全部位于「取出 `Window&` 之前」或「不再使用之后」。

**预期结果**：纪律在源码中成立；`empty_output` 的缓冲在调用者释放后的**下一次任意调用**中才真正归还（惰性回收），而非释放瞬间。

#### 4.2.5 小练习与答案

**练习 1**：调用者执行 `v = buf.view(-1)` 之后丢掉 `buf` 只留 `v`，窗口会被回收吗？

**答案**：不会。`v` 与 `buf` 共享同一 storage，引用计数仍 ≥ 2（`owned_` 里的 `Window::tensor` + `v`），`prune_owned` 的 `use_count() == 1` 不成立。这正是「按引用计数剪枝」想要的语义：只要世上还有任何视图指着这块内存，它就不能被归还。

**练习 2**：为什么 `owned_` 的值是整个 `Window`（含 `peer_ptrs`、`flag_ptrs`），而不是只存一个 tensor？

**答案**：因为零拷贝路径要**用它当传输窗口**。`prepare` 判定 `out` 是窗口后，`call.win` 就指向这条记录——后续的屏障（`flag_ptrs`）与拷贝引擎传输（`peer_ptrs`）都以它寻址。只存 tensor 的话，peer 地址与握手区地址就丢了，等于只记得「这是我的缓冲」却无法在其上执行集合传输。

**练习 3**：`destroy()` 之后调用者还持有的 `empty_output` 缓冲，为什么仍然可以安全使用（读它、对它做普通算子）？

**答案**：缓冲的内存由它自己的 storage 引用保活，`destroy()` 末尾的 `prune_owned()` 只删除 `use_count() == 1` 的记录——也就是无人持有的那些。调用者的视图使计数 ≥ 2，记录被保留、storage 不释放（[docs/api.md:165-166](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md#L165-L166)：「Buffers from `empty_output()` are unaffected — they are yours」）。当然，group 已毁，不能再把它传给新的调用。

### 4.3 out= 三态：零拷贝、拷入普通张量、默认分配

#### 4.3.1 概念说明

`all_to_all_4d(x, mode=0, out=...)` 的 `out` 参数决定这次调用的「最后一公里」怎么走。共有三种状态，其中两种最终都落在同一条拷贝路径上，只有一种真正免掉拷贝：

| # | `out` 传入什么 | `call.win` 是谁 | `out_is_window` | copy_out | 返回值 |
| --- | --- | --- | --- | --- | --- |
| 1 | 不传 | group 内部窗口 `window(role, dtype)` | `false` | **有**：窗口 → 新 `at::empty` 张量 | 新分配的普通张量 |
| 2 | 任意连续 CUDA 张量（形状/dtype/设备全对） | group 内部窗口 | `false` | **有**：窗口 → `out` | `out` 本身 |
| 3 | `empty_output()` 的缓冲 | **该缓冲自己**（`owned_` 查到） | `true` | **无**：peers 直写缓冲 | `out` 本身 |

（`lend=True` 是第四种入口，同样置 `out_is_window = true`，由 group 轮转供窗——那是 u4-l4 的主题，本讲不展开。）

三态的判据只有一个：`window_of(out)` 能否在 `owned_` 里按地址命中，且窗口字节足够。命中即零拷贝，否则走拷贝。

#### 4.3.2 核心流程

`prepare()` 中 `out` 相关的判定流程：

```text
if out 有值:
    校验: contiguous + CUDA + 设备 == group.device + dtype == x.dtype + shape == plan.output_shape
    owned = window_of(out)                      # 按地址反查 owned_
    if owned ≠ null 且 owned.tensor.nbytes() ≥ plan.window_numel × elem_size:
        win = owned; out_is_window = true       # 状态 3：零拷贝
elif lend: ...                                   # u4-l4
else:
    output = at::empty(plan.output_shape, ...)  # 状态 1：默认分配
if win 仍是 null:
    win = window(role, dtype, window_numel)     # 状态 1/2：内部窗口
重叠检查: x 与窗口字节区间不得相交; out 若非窗口本身, 也不得与窗口相交
之后: 屏障 → 传输 → 屏障 → (若非零拷贝) copy_out
```

`run()` 的收尾一行就是三态的分岔点：`if (!call.out_is_window) { copy_out(...); }`。

值得注意的两个细节：

- **字节比较而非元素数比较**：`owned_` 按地址键查找，找到的窗口 dtype 未必是本次调用的 dtype（同一地址曾被别的 dtype 的缓冲占过、或窗口是历史分配）。比较 `nbytes()` 与 `window_numel × element_size()` 才能保证装得下 peers 即将写入的字节数；若不够，条件不成立，**安静地退化为状态 2**（普通拷贝路径），行为仍正确，只是拿不到零拷贝收益。
- **copy_out 是一条平坦拷贝**：源是 `win->peer_ptrs[rank]`（本 rank 的窗口基址），长度是 `output.nbytes()`——因为 u3-l2 的不变量「本 rank 份额也走窗口，密集前缀即完整输出」。这也是它只能是**同设备**拷贝的原因：源和目的都在本卡上。

#### 4.3.3 源码精读

[bindings.cc:112-138](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L112-L138)：`prepare()` 的 `out` 分支——四条校验（contiguous CUDA、设备、dtype、shape）之后，`window_of` 反查 `owned_`；查到且 `nbytes()` 足够才置 `out_is_window = true`。L130-133 的注释解释了为什么按**字节**比：按地址找到的窗口，其分配时的 dtype 未必是本调用的，比较元素数会放进一个装不下写入字节的缓冲。

[bindings.cc:150-156](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L150-L156)：状态 1 的默认分配（`at::empty`）与状态 1/2 共同的兜底——`win` 仍为空时取 group 内部窗口 `window(role, dtype, window_numel)`（u4-l2 的按 `(role, dtype)` 缓存）。

[bindings.cc:158-172](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L158-L172)：重叠检查——输入、以及「不是窗口本身的 out」，都不得与窗口的字节区间相交，否则一边读一边被全体 peer 写。`out` **恰好是**窗口则是唯一合法的重合，即零拷贝路径。（这段检查的完整设计分析在 u6-l2，本讲只需知道它守住了单缓冲的安全边界。）

[bindings.cc:213-223](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L213-L223)：`copy_out` 全文——一条 `cudaMemcpyAsync(DeviceToDevice)`，从本 rank 的窗口基址搬到 `output`，长度是输出字节数。零拷贝路径连这一条也省掉。

[bindings.cc:225-241](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L225-L241)：`run()`——`prepare`、`transfer_on_stream`（屏障→传输→屏障，u5 的主题），然后 `if (!call.out_is_window) copy_out(...)`。**这一行就是三态的分岔点**：零拷贝调用在第二道屏障后就结束了，peers 写进的就是调用者的缓冲。

[bindings.cc:312-330](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L312-L330)：`all_to_all_4d_out` 算子——`out` 版入口，与 functional 版（[bindings.cc:253-270](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L253-L270)）只差把 `out` 传进 `run`。两个算子为何必须拆开注册，见 4.4。

[group.py:129-149](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L129-L149)：Python 门面的分派——`out is None` 走 functional 算子；有 `out` 时先做 4.4 的梯度拒绝，再调 `all_to_all_4d_out`，最后 **`return out`**：返回的就是你传入的同一个对象。

[docs/api.md:73-80](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md#L73-L80)：文档对三态的正式表述——absent 或普通张量：落进 group 保留的窗口再一条平坦 D2D 拷出，结果无生命周期约束；`empty_output()` 的缓冲：peers 直写、无 copy-out，但仍是你自己的缓冲，下一次使用它的调用会照常覆盖它。

#### 4.3.4 代码实践

**实践目标**：在三态下验证往返一致性与「零拷贝时结果就是传入的缓冲」。

**操作步骤**（需要至少 1 张 GPU，`torchrun --nproc_per_node=1` 即可；无 GPU 时按「预期结果」纸面推演并读源码标行号，标注待本地验证）：

1. 构造 group 与输入 `x`（形状按 u2-l2 契约，如 `(1, 4096, 8, 128)` bf16）。
2. 分别执行三种调用：

```python
# 示例代码（非项目自带脚本）
y1 = group.all_to_all_4d(x, mode=0)                       # 状态 1：默认分配
y2 = torch.empty_like(y1)
group.all_to_all_4d(x, mode=0, out=y2)                    # 状态 2：普通张量
buf = group.empty_output(x, mode=0)                       # 循环外、集合性分配
y3 = group.all_to_all_4d(x, mode=0, out=buf)              # 状态 3：零拷贝

assert torch.equal(y1, y2) and torch.equal(y1, y3)        # 三态结果一致
assert y3 is buf                                          # 返回的就是传入对象
assert y1.data_ptr() != buf.data_ptr()                    # 默认路径的结果不在窗口里
```

3. 再对三态各做一次 `mode 0 → mode 1` 往返，断言逐位还原（u3-l2 建立的性质）。

**需要观察的现象**：三个断言全部通过；`y3 is buf` 说明零拷贝路径没有产生新张量。

**预期结果**：三态输出位一致、往返还原。单卡（ws=1）下同样成立；多卡时形状请保持各 rank 一致（SPMD 契约）。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：状态 2 里 `copy_out` 的源地址是什么？为什么它必须是本 rank 的窗口基址？

**答案**：源是 `call.win->peer_ptrs[rank]`（[bindings.cc:218-222](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L218-L222)）。因为第二道屏障结束时，本 rank 的完整输出已经**密集地**躺在自己窗口的开头（u3-l2：本 rank 份额也走窗口），所以一条以窗口基址为源、输出字节数为长度的平坦拷贝就搬完了全部数据，不需要任何交织。

**练习 2**：如果把 `empty_output` 分配的缓冲切片后传给 `out=`（例如 `buf[:, :10]`），会发生什么？

**答案**：首先过不了形状校验（`out.sizes() != plan.output_shape`，[bindings.cc:125-129](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L125-L129)），直接 raise。假设形状恰好对上但不是完整缓冲：它不与窗口基址相等，`window_of` 查不到（或查到但视图与窗口部分重叠），会撞上「out overlaps the window this call fills without being it」的拒绝（[bindings.cc:167-172](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L167-L172)）——部分重叠既写不对也拷不对，库选择明说而不是猜。

**练习 3**：状态 2（普通张量）与状态 1（默认分配）的耗时差别在哪？

**答案**：几乎没有。两者都取内部窗口、都执行同一条 `copy_out`，唯一差别是目的缓冲由谁提供（`at::empty` vs 调用者），见 [bindings.cc:150-156](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L150-L156)。真正省时间的分界线在状态 3：整条 `copy_out` 消失。

### 4.4 不可微约定：raise 而不是静默丢梯度

#### 4.4.1 概念说明

`out=` 不可微，且库的态度是**拒绝而非装作支持**。这个约定由四层构成：

1. **schema 层**：`out` 变体是带 `Tensor(a!) out` 注解的 mutating 算子，注册时明确不给它 autograd 公式——梯度本该「从调用者拥有、且可能已被覆盖的缓冲里流出来」，这在语义上就不成立。
2. **Python 层拒绝**：`torch.is_grad_enabled() and x.requires_grad` 时直接 raise。为什么必须主动 raise：out 变体返回的就是缓冲本身，`grad_fn` 是 `None`——如果安静放行，`backward()` 会正常跑完、`x.grad` 悄悄是 `None`，调用者无从分辨「我没要梯度」和「我的梯度被丢了」。
3. **backward 永走拷贝路径**：可微形式（functional）的 backward 用的是 `A2AFunction::apply`（另一个 mode 的普通调用），绝不在 backward 里做 `empty_output` 那种集合分配——集合操作的位置必须由调用者的程序决定，不能交给 autograd 引擎的节点顺序。
4. **empty_output 自身与梯度无关**：新缓冲对形状来源张量没有数据依赖，不继承 `requires_grad`（Autograd 键 fallthrough）；同时它拒绝被 trace——重放会把集合分配放到图恰好安排的位置，而不是每个 rank 自己程序到达的位置。

#### 4.4.2 核心流程

一次 `out=` 调用在梯度维度的判定：

```text
all_to_all_4d(x, mode=0, out=buf)          # Python 门面
    if torch.is_grad_enabled() and x.requires_grad:
        raise RuntimeError("... does not support autograd ...")   # 拒绝
    调 torch.ops.fast_ulysses.all_to_all_4d_out(...)
    return buf                              # grad_fn 为 None 的普通缓冲
```

对照：functional 形式的 backward（已在 u2-l2、u6-l3 预告）：

```text
A2AFunction::backward(grads):
    return {…, A2AFunction::apply(group, grads[0].contiguous(), 1 - mode, seq, head), …}
    #        ↑ 普通的 copying 调用；不传 out，更不做集合分配
```

#### 4.4.3 源码精读

[group.py:133-143](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L133-L143)：拒绝的原文与理由——out 变体是 mutating op、无 backward，返回值又无法区分「不要梯度」与「梯度被丢」，所以 grad 模式下的 requires_grad 输入直接 raise；提示语给出两条出路：去掉 `out=`，或进 `torch.no_grad()`。

[bindings.cc:728-731](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L728-L731)：schema 注册处的注释——为什么 functional 与 out 必须是两个算子：一个 op 无法既诚实声明 `Tensor(a!)` 别名又注册 autograd 公式（torch/library 拒绝给 mutable 算子注册 autograd）；拆开后 functional 得到可微性与 Meta 内核，out 变体得到与其实际行为一致的注解。注释同时写明「Not differentiable: the gradient would have to flow back out of a buffer the caller owns and may already have overwritten」。

[bindings.cc:549-559](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L549-L559)：backward 的选择——注释说明为什么永远用 copying 形式：`empty_output` 是集合分配，放进 backward 等于把 rendezvous 位置交给 autograd 引擎的节点顺序；而且梯度属于引擎（可能累积、可能持有），窗口却是单缓冲的。`A2AFunction::apply`（而非直接调函数）保证 grad-of-grad 仍能路由回来。

[bindings.cc:753-763](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L753-L763)：`empty_output` 的注册——Autograd 键注册为 **fallthrough**，注释说明：新缓冲对形状来源张量无数据依赖，不得继承其 `requires_grad`；否则 autogradNotImplementedFallback 会在缓冲上建节点，还给调用者一个连原地 detach 都做不了的非叶子。同一段还给它注册了 vmap 拒绝。

[bindings.cc:298-308](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L298-L308)：`empty_output` 的 Meta 内核直接 raise——集合分配不能被 trace，重放会把 rendezvous 放到图安排的位置；提示调用者急切地在被 trace 区域之外调用它、再把缓冲传进去。

[docs/api.md:82-86](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md#L82-L86)：文档表述——「`out` is not differentiable, and raises rather than dropping the gradient」；推理或 `no_grad` 下接受，训练图里用普通调用。

#### 4.4.4 代码实践

**实践目标**：验证两条不可微行为——合法路径下 `grad_fn is None`，非法路径下 raise 且消息可对上。

**操作步骤**（1 张 GPU 即可；无 GPU 时纸面推演并标注待本地验证）：

```python
# 示例代码（非项目自带脚本）
buf = group.empty_output(x, mode=0)

# (a) 合法：无梯度需求下 out= 被接受，结果无 grad_fn
with torch.no_grad():
    y = group.all_to_all_4d(x, mode=0, out=buf)
assert y.grad_fn is None and y is buf

# (b) 非法：grad 模式 + requires_grad 输入 → raise
xg = x.detach().clone().requires_grad_(True)
try:
    group.all_to_all_4d(xg, mode=0, out=buf)
    raise AssertionError("should have raised")
except RuntimeError as e:
    assert "does not support autograd" in str(e)
    print("raised as documented:", str(e)[:80], "...")

# (c) 对照：functional 形式可微，backward 是另一个 mode
yf = group.all_to_all_4d(xg, mode=0)
yf.sum().backward()
assert xg.grad is not None
```

**需要观察的现象**：(a) 两个断言通过；(b) 捕获的 `RuntimeError` 消息与 [group.py:139-143](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L139-L143) 的文本一致；(c) 梯度正常回传。

**预期结果**：如上。**待本地验证**（本讲义写作环境无 GPU，未实际运行）。

#### 4.4.5 小练习与答案

**练习 1**：为什么不把拒绝做在 C++ 的 `all_to_all_4d_out` 里，而是 Python 门面？

**答案**：拒绝的判据是 `torch.is_grad_enabled()`——autograd 状态在 Python 调用现场最完整（且 functional 算子经 dispatcher 的 Autograd 键走 `A2AFunction`，out 变体根本没有 autograd 公式，C++ 侧无从区分调用者「要」还是「不要」梯度）。Python 门面是调用者与算子之间的唯一入口，在这里 raise 能给出带出路的完整消息。C++ schema 层面则以「不注册 autograd 公式 + `Tensor(a!)` 注解」表达同一事实（[bindings.cc:728-731](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L728-L731)）。

**练习 2**：`empty_output` 为什么对 Autograd 键注册 fallthrough，而不是像普通算子那样留给 autogradNotImplementedFallback？

**答案**：fallthrough 让 grad 模式下的 `empty_output(x)` 直接穿到实现层，返回一个不带节点的普通缓冲。若走 autogradNotImplementedFallback，会在缓冲上建 autograd 节点、返回非叶子张量——一个「按形状铸造出来」的缓冲被强行接进 `x` 的计算图，调用者连原地 detach 都做不到（[bindings.cc:757-761](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L757-L761) 的注释）。

**练习 3**：backward 为什么「永不使用 `out=`」，即使那时缓冲明明可以复用？

**答案**：两个独立理由（[bindings.cc:549-553](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L549-L553)）：其一，`empty_output` 是集合分配，放进 backward 意味着 rendezvous 的位置由 autograd 引擎的节点执行顺序决定，而非每个 rank 自己的程序——违反「触发分配的量/位置全 rank 一致」的地基约束；其二，梯度属于引擎，可能被累积或长期持有，而窗口是单缓冲的，持有即阻塞后续调用。

## 5. 综合实践

把本讲四条线（前缀视图、生命周期、三态、不可微）串成一个**推理循环的 A/B 实验**。这是 spec 指定的主实践。

**任务**：写一个脚本，在推理循环外用 `empty_output` 分配缓冲，循环内用 `out=` 复用；用 `group._timed` 的分段计时与 CUDA 事件总时长两组证据，确认 `copy_out` 阶段在 `out=` 写法下消失；再补上 4.4.4 的梯度用例。

```python
# 示例代码（非项目自带脚本）—— demo_out_ab.py
# 运行： torchrun --nproc_per_node=<N> demo_out_ab.py   （N=1 亦可）
import os
import torch
import torch.distributed as dist
import fast_ulysses

dist.init_process_group("nccl")
torch.cuda.set_device(int(os.environ["LOCAL_RANK"]))
group = fast_ulysses.UlyssesGroup()

b, s, n, d = 1, 4096, 8, 128          # n % ws == 0；d*2=256B 满足 16 字节对齐
x = torch.randn(b, s, n, d, device=group.device, dtype=torch.bfloat16)

# ── 证据一：_timed 给 copying 路径的四段分解 ─────────────────────────
_, stages = group._timed(x, mode=0)   # benchmark-only；读取事件会同步
print("stages:", {k: round(v, 3) for k, v in stages.items()})
assert stages["copy_out"] > 0         # copying 路径确实有搬出这一段

# ── 证据二：两种写法的端到端总时长 ───────────────────────────────────
def timeit(fn, iters=50, warmup=5):
    for _ in range(warmup):
        fn()
    torch.cuda.synchronize()
    start, end = torch.cuda.Event(True), torch.cuda.Event(True)
    start.record()
    for _ in range(iters):
        fn()
    end.record()
    torch.cuda.synchronize()
    return start.elapsed_time(end) / iters

t_copy = timeit(lambda: group.all_to_all_4d(x, mode=0))

buf = group.empty_output(x, mode=0)   # 循环外：集合分配只做一次
t_zero = timeit(lambda: group.all_to_all_4d(x, mode=0, out=buf))

print(f"copying {t_copy:.3f} ms  vs  out= {t_zero:.3f} ms")
print(f"差值 {t_copy - t_zero:.3f} ms ≈ copy_out 段 {stages['copy_out']:.3f} ms")

# ── 证据三：4.4.4 的两条不可微断言（此处省略，见 4.4.4 的脚本） ──────
```

**判读要点**：

1. `_timed` 四段的名字与顺序是 `barrier_in / transfer / barrier_out / copy_out`（[group.py:264](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L264)）。注意 **`_timed` 不接受 `out=`**——它内部固定走 copying 路径（[bindings.cc:632](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L632) 给 `prepare` 传的是 `std::nullopt`），所以「out= 下 copy_out 消失」要靠源码事实（[bindings.cc:237-239](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L237-L239) 的 `if (!call.out_is_window)`）加总时长差来共同确认，而不是指望 `_timed` 直接输出四段全零。
2. 预期 `t_copy − t_zero` 与 `stages["copy_out"]` 同量级（两次测量的流与窗口条件相同）。
3. 循环里 `out=buf` 的前提是「一个缓冲一次只喂一个调用、下一次调用前读走结果」（[docs/api.md:18-20](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md#L18-L20)）；本实验每个结果当轮即弃，满足前提。
4. ws=1 时实验同样可跑：copy_out 仍在（同卡自拷贝），`out=` 仍应消去等长时间；只是单卡上「窗口大于输出」的现象不可见（练习 4.1-1）。

**预期结果**：三组证据互相印证——copying 路径四段和为总时长；`out=` 版与 copying 版的差值 ≈ copy_out 段；梯度用例按 4.4.4 通过。本讲义写作环境无 GPU，以上**待本地验证**。

## 6. 本讲小结

- `empty_output` = 一次**集合性**对称分配（按 `window_numel` = 全组最大接收量）+ 返回其**密集前缀视图**；不均匀分片时窗口比本 rank 输出大，最大值是全 rank 免通信可算的同一个量。
- 生命周期模型：`owned_` 按窗口基址记账（`narrow` 偏移为 0 保证了视图 `data_ptr` == 基址，反查才可行），`prune_owned` 按 storage 引用计数惰性回收；调用者还持有的缓冲可比 group 长命，`destroy()` 也动不了它。
- `out=` 三态的判据是 `window_of` 能否按地址命中且字节足够：命中 → peers 直写、`copy_out` 整段消失；未命中 → 内部窗口 + 一条平坦 D2D 拷贝（目的由调用者给或默认 `at::empty`）。字节比较防住跨 dtype 命中过小缓冲。
- `out=` 不可微是四层一体的约定：schema 拆分（`Tensor(a!)` 无 autograd 公式）、Python 门面 raise（返回值无法区分「不要梯度」与「梯度被丢」）、backward 永走 copying 路径（集合分配不能交给引擎的节点顺序）、`empty_output` 对 Autograd fallthrough 且拒绝被 trace。
- 惰性回收发生在「下一次任意调用的 `prepare()` 或下一次 `make_output()` 开头」，两个剪枝点都严守「无活跃 `Window&`」纪律。

## 7. 下一步学习建议

- **u4-l4（lend=True 的窗口轮转）**：本讲反复出现的「out= 把缓冲的生死交给调用者」正是 lend 要解决的另一半——group 替你持有并轮转 4 个窗口，你会看到「选择必须是全 rank 相同的量（调用计数）、引用计数只做检查」如何直接建立在本讲的 `owned_` 模型之上。
- **u5-l1（拷贝引擎传输）**：零拷贝路径省掉的 `copy_out` 到底是什么性质的拷贝、为什么它是「遗留的同设备拷贝」，在 `launch_a2a_ce` 精读中展开。
- **u6-l2（prepare 校验流水线）**：本讲 4.3 只点到的重叠检查（为什么按字节区间而不是指针、为什么按窗口自身 nbytes）会在那里得到完整分析。
- 顺手一读：[docs/api.md](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md) 的 Collective contract 一节，把「One buffer, one call at a time」与 lend 的关系再对照一遍。
