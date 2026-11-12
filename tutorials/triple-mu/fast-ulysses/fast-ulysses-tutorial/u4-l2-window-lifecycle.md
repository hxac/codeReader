# u4-l2 窗口生命周期：按 (role, dtype) 缓存与增长

## 1. 本讲目标

上一讲（u4-l1）我们读完了 `allocate()`：一次对称内存分配如何经过 `empty_strided_p2p` + `rendezvous`，并把握手区放进 signal pad 末尾。但那讲回答的是「一次分配怎么发生」；本讲回答的是「整个生命周期怎么管理」：

1. 说清楚窗口的缓存键为什么是 `(role, dtype)` 二元组——`role` 区分 sync 与 async 两条流，`dtype` 区分元素宽度，而 **shape 故意不在键里**。
2. 解释 `window()` 的「按需增长」：容量匹配（够用就复用）取代形状匹配，每个键最多一个窗口、停在自己的高水位上。
3. 解释增长路径为什么「先 erase 再 allocate」，以及 `lend_window()`、`destroy()` 里同一个模式的再现。
4. 推导整个库最重要的一条设计约束：**触发分配的量必须全 rank 相同**——它是从 rendezvous 的顺序要求一步步推出来的，也是下一讲 lend 轮转（u4-l4）的地基。

## 2. 前置知识

- **窗口（Window）**：一个 `at::Tensor`（持有对称内存分配）加上传输需要的两张地址表——每个 peer 的数据区地址 `peer_ptrs` 和握手区地址 `flag_ptrs`。回顾 [include/fast_ulysses/group.hpp:24-29](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/group.hpp#L24-L29)，u4-l1 已逐字段讲过。
- **分配是集合操作**：`empty_strided_p2p` 在本 rank 分配，`rendezvous` 让全组交换句柄。二者必须以**相同的顺序**在所有 rank 上发生，否则有人永远等下去。这是 u4-l1 的核心结论，本讲第 4 模块会把它变成一条通用约束。
- **单缓冲（single-buffered）**：窗口只有一个缓冲区，没有双缓冲或排队。两次调用共享同一个窗口，只有当**某条流把它们排好序**（第二次的读写严格晚于第一次）时才是安全的。
- **两条互不排序的流**：sync 调用跑在调用者的当前流上；async 调用跑在 Python 侧创建的高优先级 comm 流上。CUDA 不保证这两条流上的工作有任何先后关系。
- **SPMD 调用契约**：所有 rank 发出相同的调用序列（形状序列、dtype 序列都相同）。这是 u1-l2 建立的契约，本讲会看到它直接决定了缓存键的选法。
- **storage 引用计数**：`tensor.storage().use_count() == 1` 表示「除了记录者本人，没有别人持有这块存储」——`prune_owned()` 和 `lend_window()` 都用它做检查。

## 3. 本讲源码地图

| 文件 | 本讲关注点 |
| --- | --- |
| [include/fast_ulysses/group.hpp](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/group.hpp) | `Window` 结构、`WindowRole` 枚举、`windows_` / `owned_` / `lent_` 三张表、`window()` 与 `destroy()` 的契约注释 |
| [src/group.cc](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc) | `window()`（本讲主角）、`allocate()`（u4-l1 精读，此处只取其集合性）、`lend_window()`（对照）、`destroy()`、`epoch()` |
| [src/bindings.cc](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc) | `prepare()` 里取窗口的唯一调用点；sync / async 两个入口分别传入 `kSyncWindow` / `kAsyncWindow`；`epoch_debug` 绑定 |
| [python/fast_ulysses/group.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py) | comm 流的创建处（「两条流互不排序」的原始出处）、Python 侧 `destroy()` |
| [docs/design.md](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md) | 「高水位」「每个 dtype 两个窗口」「触发分配的量必须全 rank 相同」三段设计论证 |
| [docs/api.md](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md) | 对外契约：形状序列 / dtype 序列一致、sync 与 async 不共享窗口 |
| [test/distributed/overlapping_barriers.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/overlapping_barriers.py) | sync/async 并存的正面证据，docstring 记录了共享窗口时代两个真实 bug |

## 4. 核心概念与源码讲解

### 4.1 WindowRole：单缓冲窗口与 (role, dtype) 二元组键

#### 4.1.1 概念说明

一个窗口是**单缓冲**的：同一窗口上，上一次调用的字节还没被读走，下一次调用就可以把它们覆盖掉。所以「两个调用能不能共用一个窗口」完全等价于「有没有什么东西保证第二次严格晚于第一次」。能提供这种保证的只有**流**：同一条流上的两次调用天然有序。

本库的调用恰恰分布在两条互不排序的流上：

- **sync 调用**（`all_to_all_4d`）跑在调用者的当前流上；
- **async 调用**（`all_to_all_4d_async`）跑在构造时创建的高优先级 comm 流上。

若两类调用共用一个窗口，comm 流上一次还在飞行的 async 交换（peers 正往窗口里写字节）就会和 caller 流上下一次 sync 调用的写入交错——不是性能问题，是正确性问题。于是 `WindowRole` 把「窗口属于哪条流」写进了类型：

```cpp
enum WindowRole : int64_t {
    kSyncWindow  = 0,
    kAsyncWindow = 1,
};
```

而 `windows_` 这张缓存表的键是 `std::pair<int64_t, at::ScalarType>`——`(role, dtype)`。dtype 进键的原因很朴素：分配是**带类型**的（`empty_strided_p2p` 直接吃 `ScalarType`），而 plan 发出的所有地址都是字节量、按 `elem_size` 缩放——一个 bf16 的窗口没法诚实地装一次 fp32 调用。所以**每个 dtype 有两个窗口**（sync 一个、async 一个），用几种 dtype 就是几对。

#### 4.1.2 核心流程

```text
Python: all_to_all_4d(x)          ──► bindings.cc: kSyncWindow  + 调用者当前流
Python: all_to_all_4d_async(x)    ──► bindings.cc: kAsyncWindow  + comm 流
                                        │
                       prepare(): call.win = &group->window(role, x.dtype, plan->window_numel)
                                        │
                       windows_ 以 (role, dtype) 为键查找/分配
```

- 两条路径共用同一个 `prepare()`，`role` 是参数；
- `out=`（来自 `empty_output()`）与 `lend=True` 的调用**根本不碰** role 窗口——前者用调用者自己的窗口（`owned_`），后者用轮转池（`lent_`）；
- 只有 copying 路径（无 `out`、无 `lend`）落到 `window(role, ...)`。

#### 4.1.3 源码精读

枚举与它的一段关键注释——单缓冲窗口只有在流排序下才可共享，而 sync/async 恰在不同流上：

[include/fast_ulysses/group.hpp:31-36](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/group.hpp#L31-L36)
这段定义了 `WindowRole`，注释直接说明了存在两个值的原因：窗口单缓冲，两次调用共享一个窗口的前提是「某条流把它们排序」，而 sync 与 async 调用跑在不同的流上。

两条流互不排序的原始出处，在 Python 侧创建 comm 流的地方：

[python/fast_ulysses/group.py:91-95](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L91-L95)
这里为 async 调用创建高优先级 comm 流，注释明说「两条流互不排序，这正是 C++ 侧给它们分开窗口的原因」。

sync 入口把 `kSyncWindow` 与调用者当前流一起传下去：

[src/bindings.cc:253-270](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L253-L270)
`all_to_all_4d` 算子：`require_cuda` 之后直接 `run(..., kSyncWindow, at::cuda::getCurrentCUDAStream(), /*lend=*/false)`——role 与流在这里配对出现。

async 入口则先 `stage()` 再以 `kAsyncWindow` 走同一个 `run()`：

[src/bindings.cc:385-396](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L385-L396)
staged 路径的收尾一行：`return run(group, slot->tensor, ..., kAsyncWindow, comm, lend)`——同一个 `run()`，不同的 role 与不同的流。

`prepare()` 里取窗口的唯一调用点（`out=`/`lend` 路径已提前拿到的窗口会跳过它）：

[src/bindings.cc:154-156](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L154-L156)
`call.win = &group->window(role, call.x.scalar_type(), call.plan->window_numel)`——role、dtype、所需容量三个参数一起进 `window()`。

设计文档里的一句话结论：

[docs/design.md:67-69](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md#L67-L69)
「每个 dtype 两个内部窗口，每条跑集合通信的流一个；窗口是单缓冲的，两次调用只有被流排序时才能共享。」

#### 4.1.4 代码实践

**实践目标**：在 `world_size=1` 下证明 sync 窗口与 async 窗口是**两个并存**的分配，而不是同一个。

**操作步骤**：

1. 依赖 `epoch()` 的一个可从源码直接推出的性质：它按 `(role, probe 的 dtype)` 查 `windows_`，查不到时返回 `-1`（见 [src/group.cc:424-433](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L424-L433)）。而 `world_size=1` 时屏障被整体跳过（[src/barrier.cu:53-57](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/barrier.cu#L53-L57)），所以**已存在**的窗口 epoch 恒为 0。
2. 于是 `-1` 与 `0` 就成了「窗口不存在 / 窗口已存在」的探测器。调用约定参照 [test/distributed/window_race.py:187](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/window_race.py#L187) 的 `group._handle.epoch_debug(probe, 0)`。
3. 写三行探测（示例代码，需单卡 GPU）：

```python
# 示例代码
x = torch.randn(1, 1024, 8, 64, dtype=torch.bfloat16, device=group.device)
print(group._handle.epoch_debug(x, 0))   # 任何调用之前：-1（sync 窗口不存在）
group.all_to_all_4d(x, mode=0)
print(group._handle.epoch_debug(x, 0))   # 0：sync 窗口已存在（ws=1 下 epoch 不推进）
print(group._handle.epoch_debug(x, 1))   # -1：async 窗口仍然不存在
group.all_to_all_4d_async(x, mode=0).wait()
print(group._handle.epoch_debug(x, 1))   # 0：async 调用后第二个窗口出现
```

**需要观察的现象**：四个打印值依次为 `-1, 0, -1, 0`。

**预期结果**：`role=0` 从 `-1` 变 `0` 不影响 `role=1` 仍是 `-1`——`(kSyncWindow, bf16)` 与 `(kAsyncWindow, bf16)` 是 `windows_` 里两个独立的键、两份独立的对称内存。具体数值属「待本地验证」，但 `-1`/`0` 的判别逻辑完全由上面两处源码决定。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `WindowRole` 删掉、每个 dtype 只留一个窗口，最先出事的不是数据而是握手——为什么？

**答案**：握手状态（`flags[ws]` + `epoch`）住在每个窗口自己的 signal pad 里。两个并存调用若共用一个窗口，就共用一个 epoch 计数：两个 barrier 内核可能领到同一个 epoch，其中一个握手变成空操作，落后的 rank 永远等不到满足条件的 flag，整组挂起。这正是 [test/distributed/overlapping_barriers.py:14-23](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/overlapping_barriers.py#L14-L23) docstring 记录的两个历史 bug（共享 epoch → 挂起；epoch 唯一但两流间顺序不一致 → 结果撕裂）。

**练习 2**：`out=` 调用和 `lend=True` 调用会碰 role 窗口吗？

**答案**：不会。`prepare()` 里 `out=` 来自 `empty_output()` 时直接用调用者自己的窗口（`owned_` 里的记录），`lend=True` 时用 `lend_window()` 的轮转池；两者都置 `call.out_is_window` 并跳过 [src/bindings.cc:154-156](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L154-L156) 这行的兜底取窗。role 窗口只服务 copying 路径。

**练习 3**：为什么 dtype 必须进键，而 `mode`（0/1）不需要？

**答案**：dtype 决定元素宽度，plan 的所有字节量都按 `elem_size` 缩放，窗口分配也带类型（[src/group.cc:218-219](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L218-L219)）；不同 dtype 的字节宽度不同，无法安全混装。而 mode 只影响**布局**（读写步长），不影响窗口要装多少元素——`window_numel` 对两种 mode 都取全组最大接收量，容量够就都能用。

### 4.2 按需增长：window() 的容量匹配与高水位

#### 4.2.1 概念说明

键里没有 shape，那不同形状的调用怎么共用一个窗口？答案是**容量匹配**：窗口记住了自己的容量 `numel`，一次调用只要所需的 `window_numel` 不超过它，就直接复用；超过才换一个更大的。效果是：

- 每个 `(role, dtype)` 键最多一个窗口，大小停在历史最大需求上——「高水位」（high-water mark）；
- 窗口**只增长、不收缩**：小形状的后续调用复用大窗口，多余容量闲着；
- 形状序列可以任意多样（数据相关的形状也没关系），因为匹配标准是容量而非形状。

对比一下另外两张缓存表，能看出这是刻意的设计取舍：plan 缓存 `plans_` 按完整形状做键、条目多了整体清空（[src/group.cc:188-190](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L188-L190)，512 条上限）；暂存池 `staging_` 按 `(shape, dtype)` 做键、16 个形状封顶。这两张表存的是**轻量东西**（几微秒就能重建的 host 向量、几个输入大小的缓冲），所以「按形状精细分桶 + 到顶清空」可接受。窗口不一样：每次分配都要付一次集合 rendezvous，而且桶数不封顶的话，每个曾出现过的形状都永久钉住一块对称内存——`windows_` **没有任何淘汰逻辑**，能这么放心，正是因为键只有 `2 × dtype 数` 那么少（dtype 白名单 7 种，理论上限 14 个窗口）。

顺带把上一讲的伏笔接上：`window_numel` 之所以是「全组各 rank 输出的最大值」（u3-l1、u3-l2），正是为了让「所需容量」成为一个全 rank 相同的量——它从 plan 来，与谁在调用无关。这一点在 4.4 还要用。

#### 4.2.2 核心流程

```text
window(role, dtype, numel):
    key = (role, dtype)
    hit = windows_.find(key)
    if hit 存在 且 hit.numel >= numel:   # 容量够 → 复用，零开销，非集合
        return hit
    if hit 存在:
        windows_.erase(hit)              # 先释放旧窗口（见 4.3）
    windows_[key] = allocate(numel, dtype)   # 集合操作：分配 + rendezvous + 清零握手区
    return 新窗口
```

关键性质：

- **命中路径不是集合操作**——只是查一次 `std::map`；
- **未命中路径是集合操作**——所有 rank 对同一个调用得出同一个「未命中」结论（原因见 4.4），于是所有 rank 一起进 `allocate()`；
- `numel` 单调不降：同一个键上的窗口容量只会被更大的需求抬升。

#### 4.2.3 源码精读

主角全文只有 13 行：

[src/group.cc:255-267](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L255-L267)
`window()` 的全部逻辑：先 `check_alive()`（destroy 之后拒绝），按 `(role, dtype)` 组键查找；第 260 行 `it->second.numel >= numel` 就是容量匹配——**不比较形状，只比较元素数**；装得下就返回，装不下先删旧的再 `allocate(numel, dtype)` 装进表里。

头文件里 `window()` 的契约注释：

[include/fast_ulysses/group.hpp:82-84](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/group.hpp#L82-L84)
「首次使用时分配、后续调用需要更多时增长；**分配时是集合操作**」——注意措辞：增长也写明 COLLECTIVE，因为它同样要走 `allocate()`。

三张表并排放在私有成员里，键的粗细一目了然：

[include/fast_ulysses/group.hpp:175-177](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/group.hpp#L175-L177)
`plans_`（完整形状做键）、`windows_`（`(role, dtype)` 做键）、`owned_`（按地址记调用者持有的窗口）三张表排在一起——窗口表的键最粗，故无需淘汰。

设计文档对「高水位」的表述：

[docs/design.md:61-65](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md#L61-L65)
「内存不预先提交、也不单调增长：丢掉一个窗口会把它还给对称分配器。窗口按容量匹配、需要更多时增长，所以**一个调用点只付一个停在高水位上的窗口**。」

对外契约把这件事翻译成用户的义务：

[docs/api.md:10-14](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md#L10-L14)
「每个 rank 必须发出相同的形状序列」「相同的 dtype 序列」——因为窗口在首次需要时分配、且分配是集合操作；形状与 dtype 正是决定「这次调用落在哪个键、要不要增长」的量。

#### 4.2.4 代码实践

**实践目标**：在 `world_size=1` 下用手算的 `window_numel` 预测「先小、再小、再大、再小」四次调用中哪几次触发分配。

**操作步骤**：

1. 手算容量。`world_size=1`、`mode=0` 时输出形状就是 `(b, s, n/ws, d) = (b, s, n, d)`，故 `window_numel = b·s·n·d`：
   - 小形状 `x_small = (1, 1024, 8, 64)`，bf16 → `window_numel = 524288` 个元素 = 1 MiB；
   - 大形状 `x_big = (1, 4096, 8, 64)`，bf16 → `window_numel = 2097152` = 4 MiB。
2. 写下预测（依据就是 [src/group.cc:260](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L260) 那一行比较）：
   - 第 1 次 small：未命中 → 分配 1 MiB；
   - 第 2 次 small：命中（524288 ≤ 524288）→ 不分配；
   - 第 3 次 big：未命中（524288 < 2097152）→ 换 4 MiB 窗口；
   - 第 4 次 small：命中（524288 ≤ 2097152）→ 不分配，复用大窗口。
3. 把这个预测写成一棵注释树，标注每步命中/未命中与窗口容量。这棵树就是第 5 节综合实践里要拿实跑验证的假设。

**需要观察的现象**：见第 5 节综合实践——用显存计数与 `epoch_debug` 两个探针对拍。

**预期结果**：第 2、4 次调用不产生任何新的对称内存分配；第 4 次复用 4 MiB 窗口即是「只增长不收缩」的直接证据。**待本地验证**（本练习只需纸面完成预测部分）。

#### 4.2.5 小练习与答案

**练习 1**：训练循环里形状每步都在变（比如可变 batch），窗口会不会越积越多？

**答案**：不会。键是 `(role, dtype)`，与形状无关；容量匹配只认「够不够」。形状变大时窗口被换成更大的，旧窗口当场释放；形状变小或抖动时复用现有窗口。最终每个键一个窗口、停在这条调用流的高水位上。

**练习 2**：既然「只增长不收缩」，一个曾经用过 4 MiB 窗口、此后永远只跑 1 MiB 调用的组，会不会一直白占 3 MiB？

**答案**：会，直到 `destroy()`。`windows_` 没有缩容路径，也没有空闲超时回收。这是用「键少 + 分配集合性贵」换来的简单性；想立即归还，只能 `destroy()` 整组重建（[src/group.cc:475](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L475) 的 `windows_.clear()`）。

**练习 3**：`plans_` 有 512 条清空上限、`staging_` 有 16 形状上限，`windows_` 为什么敢一个上限都不设？

**答案**：因为键空间本身有界：role 只有 2 个值，dtype 白名单 7 种（[src/group.cc:59-62](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L59-L62)），最多 14 个键。另外两张表的键含完整形状，形状可以无限多样，不设上限就是无界内存。

### 4.3 先释放后分配：增长路径的顺序、lend_window 对照与 destroy

#### 4.3.1 概念说明

增长路径有一个只有一行、却值得单开一节讲清的顺序决定：**先把旧窗口从表里删掉（释放其对称内存），再去 allocate 新的**。若反过来——先分配新的、再释放旧的——会有两个后果：

1. 峰值时刻同时占住新旧两块对称内存；而新窗口严格更大，进程对对称内存的瞬时占用翻倍还多；
2. 旧段来不及参与复用：先释放，分配器在处理新请求时手上已经握着刚交回的段，可以直接拿去满足（或参与切分）；后释放，这次分配只能另找内存，旧段沦为碎片。

同一个模式在 `lend_window()` 的增长分支里原样再现（先 `slot = Window{}` 置空释放，再 `allocate`），注释还专门点名「与 `window()` 取相同的顺序、出于相同的原因」。这说明它不是巧合，而是一条被提炼过的规则。

本模块顺带读完生命周期的终点 `destroy()`：它释放 xfer 流、staging 事件、三张缓存表，但**调用者手里的 `empty_output()` 缓冲不受影响**——那些缓冲靠自己的 storage 引用活着，组只持有 `owned_` 里的记录，删记录不删内存。

#### 4.3.2 核心流程

```text
window() 增长：
    windows_.erase(旧)      # ① 旧窗口的 Window 析构 → tensor 引用归零 → 对称内存还给分配器
    allocate(更大)           # ② 分配器此时已握有 ① 交回的段，可复用

lend_window() 槽位增长（同一模式）：
    slot = Window{}          # ① 先把槽位置空 → 释放
    slot = allocate(更大)    # ② 再分配

destroy()：
    destroyed_ = true
    同步并销毁 xfer 流 → 销毁 staging 事件 → clear plans_ / windows_ / lent_ → prune_owned()
    （owned_ 只删「没人再持有」的记录；调用者还握着的缓冲靠自身 storage 存活）
```

#### 4.3.3 源码精读

那一行注释点破动机：

[src/group.cc:263-265](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L263-L265)
`windows_.erase(it)` 旁的注释：「先释放再分配，让分配器能复用该段（free before allocating, so the allocator can reuse the segment）」。erase 会析构旧 `Window`，其 `tensor` 成员的引用归零，对称内存回到分配器。

`lend_window()` 增长分支的同一模式：

[src/group.cc:297-300](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L297-L300)
`slot = Window{};` 先置空（析构旧窗口、释放内存），紧接着 `slot = allocate(numel, dtype)`，行尾注释明说这是 `window()` 采用的顺序、理由相同。

`destroy()` 全文：

[src/group.cc:453-482](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L453-L482)
`destroy()` 先置 `destroyed_`（幂等：开头两行让重复调用直接返回），再同步并销毁 xfer 流（teardown 里不做错误检查，注释说明调用方已让组静默），销毁 staging 事件，清空 `plans_` / `windows_`（第 475 行）/ `lent_`，最后 `prune_owned()`。结尾注释点明：调用者仍握着的缓冲经它们自己的 storage 引用继续存活，这里释放的只是没人保留的那些。

`owned_` 的剪枝逻辑（destroy 最后一行调用的函数）：

[src/group.cc:198-205](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L198-L205)
`prune_owned()` 遍历 `owned_`，storage 引用计数为 1（只剩记录者自己）的条目被删除——此时分配真正归还。`lend_window()` 第 289 行对轮转槽位用的是同一个判据。

Python 侧 `destroy()` 的收尾顺序：

[python/fast_ulysses/group.py:267-277](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L267-L277)
先 `self._comm_stream.synchronize()`（`dist.barrier` 只同步调用者的当前流，不同步 comm 流的话，一次未等待的 async 调用可能仍在写一个即将被释放的窗口），再 `dist.barrier`，最后 `_handle.destroy()`。docstring 也点明：释放窗口是集合操作，所有 rank 要一起调。

#### 4.3.4 代码实践

**实践目标**：把「先释放后分配」从一句注释变成可指认的代码事实，并弄清 `destroy()` 释放什么、不释放什么。

**操作步骤**：

1. 打开 `src/group.cc`，找到 [第 263-265 行](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L263-L265) 与 [第 297-300 行](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L297-L300)，给两处各加一条自己的注释（写在你的笔记里，不改源码）：「erase/置空在前 → 引用归零 → 对称内存回池 → allocate 可复用」。
2. 回答并写下：如果把 `window()` 第 263-265 行与第 266 行对调（先 `allocate` 再 `erase`），在哪两个瞬间进程的对称内存占用变大？旧段是否还可能被新分配复用？
3. 读 `destroy()`（[src/group.cc:453-482](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L453-L482)）后回答：调用者先 `buf = group.empty_output(x, mode=0)`、再 `group.destroy()`，`buf` 还能用吗？依据是哪几行？

**需要观察的现象**：无运行内容，纯源码阅读；判断能否自圆其说。

**预期结果**：

- 对调后：分配新窗口的瞬间新旧两块同时在握（峰值更高），且新分配发生时旧段尚未回池、无法参与复用；
- `buf` 仍可用：`destroy()` 第 481 行 `prune_owned()` 只删 `owned_` 里引用计数为 1 的记录，`buf` 自己持有一份 storage 引用（计数 ≥ 2），删除记录不触发释放；[docs/api.md:163-166](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md#L163-L166) 也明说「`empty_output()` 的缓冲不受影响——它们是你的」。

#### 4.3.5 小练习与答案

**练习 1**：`window()` 里 `erase` 与 `emplace` 之间没有任何同步，会不会有别的线程在「无窗口」的空档用到这个键？

**答案**：常规使用下不会。窗口的取用发生在 `prepare()` 里，而 `prepare()` 由 torch dispatcher 的算子调用进入；SPMD 契约本身就要求所有 rank 串行地发出同一个调用序列，库也从不从别的线程碰 `windows_`。这两行的顺序决定只关心**分配器视角**的先来后到（旧段先回池），不关心并发。

**练习 2**：`destroy()` 里为什么 `windows_.clear()` 不会误伤 lend 池里「调用者还握着的结果」？

**答案**：lent 结果是窗口张量的视图，调用者持有它就多了一份 storage 引用；`lent_.clear()` 析构池里的 `Window` 只是让引用计数减一，只要调用者的结果还活着，那块对称内存就不会真正归还。[src/group.cc:477-478](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L477-L478) 的注释说的正是这件事。

**练习 3**：`destroy()` 之后误调 `all_to_all_4d` 会发生什么？在哪个检查被拦下？

**答案**：抛 `RuntimeError`（"this UlyssesGroup has been destroyed"）。`window()` 第一行调 `check_alive()`（[src/group.cc:257](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L257)），但更早的拦截在 `prepare()` 开头（[src/bindings.cc:99-102](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L99-L102)）：零拷贝路径两个取窗函数都到不了，没有这个前置检查的话，一个已销毁的组会把刚拆掉的传输流又重建起来、跑完整场集合通信。

### 4.4 全 rank 一致的分配触发：一条从 rendezvous 推出的设计约束

#### 4.4.1 概念说明

现在把 u4-l1 的 rendezvous 论证推到尽头。回顾前提：`allocate()` 直连分配入口点（不挂 MemPool），所以**没有任何无关分配能插进我们的序列**；于是「本组做出的分配序列」就是「每个 rank 做出的分配序列」——前提是每个 rank 做出**相同的分配决策**。

把决策写成一个函数：一次调用要不要分配、分配多大，是 \(\text{分配决策} = f(\text{该次调用的属性})\)。rendezvous 要求所有 rank 对第 \(i\) 次调用得到同一个答案。于是：

> **触发分配的量必须全 rank 相同。**

形状、dtype、mode、splits、调用计数、plan 算出的 `window_numel`——都是 SPMD 契约下每个 rank 独立可算且必然一致的量。而引用计数、本 rank 的空闲内存、到达时间——都是局部量，各 rank 不同。

逐条验证本讲的代码：

- `window()` 的三个输入 `role / dtype / plan->window_numel` 全部来自当次调用与它的 plan，一致；
- 增长的触发条件 `it->second.numel >= numel` 两侧也都是一致量（窗口当前容量由历史一致决策累积而来，`numel` 来自 plan）；
- 对照组 `lend_window()`：选槽用 `pool.calls++ % kLentSlots`——**调用计数**是一致量；而引用计数只出现在 TORCH_CHECK 里做**检查**，从不参与选择（这正是 u4-l4 的主题，此处只看结构）。

反例推演：假如让 `window()` 在「本 rank 恰好有空闲窗口」时才分配、否则复用（一个读局部状态的决策），两个 rank 的执行顺序稍有错开就会在同一调用上一个进 `allocate()`、一个直接复用——进 `allocate()` 的 rank 在 rendezvous 里等一个永远不会来的伙伴，整组挂起，且没有任何报错。

#### 4.4.2 核心流程

```text
SPMD 契约：所有 rank 执行相同调用序列 C1, C2, ...
    ↓
每次 Ci 的 (shape, dtype, mode, splits) 全 rank 相同
    ↓
plan(Ci) 的 window_numel 全 rank 相同
    ↓
window()/lend_window() 的分配决策只读这些量 → 决策全 rank 相同
    ↓
所有 rank 以相同顺序到达相同数量的 rendezvous → 不挂起
```

任何往决策函数里塞入局部量（引用计数、空闲内存、时间）的改动，都会打断这条链。

#### 4.4.3 源码精读

设计文档的原文，一段把整条约束说完：

[docs/design.md:71-77](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md#L71-L77)
「这个顺序论证约束了后面每条按需分配的路径：**触发分配的必须是一个每个 rank 都共享的量**。形状、dtype、调用计数——这些处处相同，因为 SPMD 契约说调用序列就是；引用计数不是，任何由它派生的东西也不是。`lend=True` 是这条约束咬人的地方：按调用计数挑窗口，而不是找一个没人握着的窗口，恰恰因为后者读的是局部状态。引用计数仍会出现——作为轮转落点是否空闲的检查，永远不是选择。」

`allocate()` 直连入口点、排除无关分配插队的那两行（u4-l1 讲过，此处是约束的起点）：

[src/group.cc:216-219](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L216-L219)
注释：「用 `empty_strided_p2p` 而非 MemPool：它是分配器入口点本身，任何无关分配都无法与我们的交错、打乱各 rank 的 rendezvous 顺序。」

`window_numel` 为什么天生全 rank 一致——源头在 plan（承接 u3-l1/u3-l2 的结论），入口在 bindings：

[src/bindings.cc:154-156](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L154-L156)
传给 `window()` 的不是本 rank 的输出大小，而是 `plan->window_numel`——plan 取的是全组各 rank 输出的最大值，本 rank 与任何 rank 算出的都是同一个数。

`lend_window()` 里「一致量做选择、局部量做检查」的分工：

[src/group.cc:277-296](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L277-L296)
第 280 行 `const size_t idx = pool.calls++ % kLentSlots;` 用调用计数选槽（注释：计数器，绝不搜索空闲槽——理由见头文件）；第 289-296 行的 TORCH_CHECK 用引用计数做检查并在违规时抛错——注意它**抛出而不是退化成拷贝**，注释说「退化也是本 rank 单方面做的决定」，同样是违反一致性。

头文件里对「为什么必须用计数器」的完整论证：

[include/fast_ulysses/group.hpp:96-106](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/group.hpp#L96-L106)
「槽由调用计数选择，绝不靠找一个空闲的……每个 rank 必须落在同一个对称分配上：传输经由所选窗口的 `peer_ptrs` 寻址对端、屏障经由它的 `flag_ptrs`，意见不合的 rank 会互相写穿、等一个没人置位的 flag。本 rank 自己的引用计数不是对端共享的量；它发出的调用次数是，因为每个 rank 发出相同序列。」

#### 4.4.4 代码实践

**实践目标**：把「一致量 / 局部量」的判别变成可以批量执行的动作。

**操作步骤**：

1. 列两张清单。对下列每个量判断它是否「全 rank 相同」，并注明出处：
   - `x.sizes()`、`x.scalar_type()`、`mode`、`seq_splits` / `head_splits`；
   - `plan->window_numel`、`pool.calls`（lend 池的调用计数）；
   - `slot.tensor.storage().use_count()`、`torch.cuda.memory_allocated()`、某次调用到达 `prepare()` 的墙钟时刻。
2. 对三个「局部量」，各写一句反例推演：把它接进 `window()` 的决策（比如「空闲内存低于阈值就先不增长、复用旧窗口硬写」），描述两个 rank 在什么执行交错下决策分歧、卡死在哪个调用里。
3. 用 `git log --oneline -- src/group.cc | head -20` 浏览 `group.cc` 的提交历史，找出 lend 轮转（`feat: lend the output window...`）那次提交，`git show` 它，确认引入的决策量是计数器而非引用计数。

**需要观察的现象**：清单分类是否与 [docs/design.md:71-77](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md#L71-L77) 的划分完全吻合；提交历史里是否能看到约束被遵守的痕迹。

**预期结果**：一致量 = 形状、dtype、mode、splits、window_numel、调用计数；局部量 = 引用计数、空闲内存、到达时刻。第 3 步的提交（对应 0.2.1 版本的 lend 功能）里选槽逻辑是 `calls++ % kLentSlots`，引用计数仅出现在 TORCH_CHECK。git 命令在本仓库只读可用；具体输出**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`window()` 的复用条件里有一侧是「窗口当前容量」——这看起来是组的历史状态，为什么它也算全 rank 一致？

**答案**：归纳法。初始时所有 rank 的 `windows_` 为空（一致）；此后每一步的决策函数只读一致量，故每步的分配/复用结论一致，于是每步结束后每个 rank 的 `windows_` 内容（键集合与每个键的容量）仍然一致。一致的输入 + 一致的函数 = 一致的状态。

**练习 2**：引用计数做检查失败时，`lend_window()` 为什么选择抛异常，而不是「这次退回 copying 路径加一次 copy_out」？

**答案**：因为「退回拷贝」是本 rank 单方面能做的决定，而其他 rank 仍在轮转语义下前进——两侧对「这次调用写哪个窗口」的认知分叉，等于亲手制造了 4.4.1 的挂起场景。抛异常是唯一不需要对端配合的失败方式。[include/fast_ulysses/group.hpp:104-106](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/group.hpp#L104-L106) 的注释原文：「退回拷贝也是局部决定」。

**练习 3**：`empty_output()` 每次调用都真的分配一次新窗口（不缓存、不增长），它不怕违反本约束吗？

**答案**：不怕——它把约束转嫁给了调用者：分配本身就是集合操作（文档明说 COLLECTIVE，须在循环外调用），「要不要分配」由用户程序的调用序列决定，而 SPMD 契约本来就要求各 rank 的调用序列一致。约束没有被绕过，只是从「库内部读调用参数」换成了「用户直接表达」。

## 5. 综合实践

把三个问题与一个实验合起来做（这是本讲规格里的主实践任务）。

**任务**：写一个带注释的 `window()` / `destroy()` 阅读笔记 + 一个 `world_size=1` 的「只增长不收缩」验证脚本。

**第一步（阅读与作答）**：精读 [src/group.cc:255-267](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L255-L267)（`window()`）与 [src/group.cc:453-482](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L453-L482)（`destroy()`），写出三段注释，各回答一个问题：

1. **为什么按 `(role, dtype)` 而不是按 shape 缓存？** role 在键里是因为窗口单缓冲，只有同一条流排序的调用才能共享，而 sync（调用者流，[src/bindings.cc:267-268](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L267-L268)）与 async（comm 流，[python/fast_ulysses/group.py:91-95](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L91-L95)）互不排序；dtype 在键里是因为分配带类型、字节寻址随元素宽度缩放；shape 不在键里是因为容量匹配已经取代了它——按 shape 分桶会让每个出现过的形状永久钉住一块对称内存（`windows_` 无淘汰），而按容量只需每个键一个高水位窗口。
2. **增长时为何先 `windows_.erase` 再 `allocate`？** 先释放让分配器在新请求到来时已握有旧段、可直接复用；同时避免新旧两块同时占用的峰值。[src/group.cc:264](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L264) 的注释与 [src/group.cc:298-299](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L298-L299) 的同型代码互为印证。
3. **如果 sync 与 async 共用一个窗口，哪两个流的调用会互相覆盖？** 调用者当前流上的 sync 调用与高优先级 comm 流上的 async 调用：async 的 peers 还在往窗口写字节时，caller 流的下一次 sync 调用已经在写同一块内存（读写交错、结果撕裂），两个 barrier 内核还会争同一份 epoch。[test/distributed/overlapping_barriers.py:1-34](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/overlapping_barriers.py#L1-L34) 的 docstring 记录了共享窗口时代的两个真实 bug（挂起与撕裂），[docs/api.md:149](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md#L149)「sync 与 async 不共享窗口，所以混用是安全的」正是靠两窗口成立。

**第二步（实跑验证，示例代码，需单卡 GPU）**：

```python
# grow_only_probe.py -- torchrun --nproc_per_node=1 grow_only_probe.py
import torch
import torch.distributed as dist
from fast_ulysses import UlyssesGroup

dist.init_process_group("nccl")
torch.cuda.set_device(dist.get_rank())
group = UlyssesGroup()
dev = group.device

def mem(tag):
    torch.cuda.synchronize()
    print(f"{tag:>16s}: {torch.cuda.memory_allocated(dev) / 2**20:8.1f} MiB")

small = torch.randn(1, 1024, 8, 64, dtype=torch.bfloat16, device=dev)  # window_numel 1 MiB
big   = torch.randn(1, 4096, 8, 64, dtype=torch.bfloat16, device=dev)  # window_numel 4 MiB

print("before anything   sync:", group._handle.epoch_debug(small, 0),   # 预期 -1
      " async:", group._handle.epoch_debug(small, 1))                   # 预期 -1
mem("start")
group.all_to_all_4d(small, mode=0); mem("small #1")   # 未命中：分配 1 MiB 窗口
group.all_to_all_4d(small, mode=0); mem("small #2")   # 命中：只多一个输出张量
group.all_to_all_4d(big,   mode=0); mem("big    ")    # 增长：-1 MiB 旧 +4 MiB 新窗口
group.all_to_all_4d(small, mode=0); mem("small #3")   # 命中：复用 4 MiB 窗口 ← 只增不减
group.all_to_all_4d_async(big, mode=0).wait(); mem("async  ")           # 第二个窗口出现

print("after everything  sync:", group._handle.epoch_debug(small, 0),   # 预期 0（存在；ws=1 不推 epoch）
      " async:", group._handle.epoch_debug(small, 1))                   # 预期 0（此前 -1）

# 往返位精确抽查：数据没被任何一次窗口复用弄脏
assert torch.equal(group.all_to_all_4d(group.all_to_all_4d(small, mode=0), mode=1), small)

group.destroy()
dist.destroy_process_group()
```

**判读**（对照 4.2.4 的预测）：

- `small #2` 的增量应明显小于 `small #1`——差的正是一个窗口；
- `small #3` 的增量与 `small #2` 同级、远小于 `big`——大窗口被复用，这就是「只增长不收缩」；
- `async` 之后 `epoch_debug(..., 1)` 从 `-1` 变 `0`——`(kAsyncWindow, bf16)` 是与 sync 窗口并存的第二个分配；
- 最后的 `torch.equal` 断言通过——多次复用同一窗口没有弄脏数据。

**注意**：copying 路径每次调用还会 `at::empty` 一个普通输出张量、async 首次调用还会建 staging 缓冲，所以各步增量里混有这些成分，对拍时看「同级形状之间的差」而非绝对值；若你的 torch 版本未把对称内存计入 `memory_allocated`，可改用 `torch.cuda.mem_get_info()` 观察同样的定性现象。具体数字**待本地验证**。

## 6. 本讲小结

- 窗口缓存键是 `(role, dtype)`：role 区分 sync（调用者流）与 async（comm 流）两条互不排序的流——单缓冲窗口只有被同一条流排序的调用才能共享；dtype 决定元素宽度与字节寻址；shape 被容量匹配取代，故 `windows_` 键少到无需任何淘汰。
- `window()` 只认容量：`numel >= 所需` 即复用（命中路径非集合、零开销），不足则增长；每个键一个窗口、停在高水位，只增长不收缩，直到 `destroy()`。
- 增长路径先 `erase` 再 `allocate`，让分配器在处理更大请求前先握回旧段——`lend_window()` 的槽位增长采用完全相同的顺序，注释互指。
- `destroy()` 清三张表与 xfer 流，但调用者手里的 `empty_output()` 缓冲靠自身 storage 引用存活，`prune_owned()` 只回收没人再持有的记录。
- 最重要的设计约束：**触发分配的量必须全 rank 相同**。它由 rendezvous 的顺序要求推出：一致量（形状、dtype、window_numel、调用计数）可进决策，局部量（引用计数、空闲内存、时间）只能做检查、绝不能做选择——违反的代价是整组挂起而非报错。

## 7. 下一步学习建议

- 下一讲 **u4-l3（empty_output 与调用者持有的缓冲）**：`owned_` 表与零拷贝 `out=` 路径——本讲 4.3 里 `prune_owned` 的判据将在那里展开成完整的三态 `out=` 语义。
- 再下一讲 **u4-l4（lend=True 窗口轮转）**：把本讲 4.4 的约束用到极致——`kLentSlots=4` 的按计数轮转、引用计数只做检查、第五个存活结果抛错。
- 想看两窗口并存的正面证据与历史 bug，直接读 [test/distributed/overlapping_barriers.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/overlapping_barriers.py) 的 docstring（u9-l3 会精读它）。
- 窗口生命周期与 CUDA graph 捕获边界的交点（「窗口已存在」的 sync 调用才可捕获）在 [docs/design.md:91-101](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md#L91-L101)，u5-l3 展开。
