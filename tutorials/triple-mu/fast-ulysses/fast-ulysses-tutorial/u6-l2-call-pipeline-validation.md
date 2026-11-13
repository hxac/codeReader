# 一次调用的完整流水线：prepare → 屏障 → 传输 → 屏障 → copy_out

## 1. 本讲目标

学完本讲，你应该能够：

1. 按执行顺序列出 `prepare()` 中的全部检查，并解释它们的排序原则——**一切校验先于第一道屏障**，被拒的参数不给任何 rank 留下等待。
2. 说清字节区间重叠检查的三个细节：地址查找（`window_of` 按地址命中）、按字节比较（而非按元素个数）、例外路径（`out` 就是窗口时豁免）。
3. 解释 `transfer_on_stream()` 里「开路屏障 → CE 传输 → 收尾屏障」各自保护什么，以及 `copy_out()` 为什么是一条平坦拷贝。
4. 对 copying / zero-copy / lend 三条路径，分别追踪 `output` 与 `win` 两个缓冲的来源，说清 `copy_out` 何时存在、何时消失。

本讲是 u6-l1（算子注册）的落地篇：上一讲看的是「这些 op 如何注册进 dispatcher」，本讲看「一次调用进来后，内核体内到底按什么顺序发生什么」。

## 2. 前置知识

本讲站在以下已建立的认知之上（对应 u4、u5 各讲）：

- **窗口（Window）与单缓冲语义**：窗口是 torch 对称内存分配（`empty_strided_p2p` + `rendezvous` 的产物），含 `tensor`、`peer_ptrs`、`flag_ptrs`、`numel` 四个成员；窗口按 `(role, dtype)` 缓存（u4-l2），`kSyncWindow` 服务同步调用所在的调用者流，`kAsyncWindow` 服务异步调用的 comm 流。单缓冲意味着：本次调用会直接覆写窗口里上一次调用的产物。
- **owned_ 与 empty_output**：`empty_output()` 分配一个窗口、按地址登记进 `owned_` 表，把密集前缀视图交给调用者（u4-l3）；调用者把它作为 `out=` 传回时，peers 直写该缓冲、`copy_out` 消失。
- **lend 轮转**：`lend=True` 由调用计数选槽出借窗口（u4-l4），结果是窗口密集前缀的视图，同样没有 `copy_out`。
- **CE 传输与标志位屏障**：拷贝引擎（CE）按 `build_plan` 生成的 `CopyOp` 表执行带步长的 peer 拷贝（u5-l1）；每对 rank 之间用单块自旋内核做发布-等待握手（u5-l3）。
- **挂起而非报错**：集合操作中，如果某个 rank 抛异常退出而其他 rank 已进入屏障/集合分配，后者会在 GPU 自旋或 NCCL 等待中永久卡住（u2-l2 的契约五条）。这是本讲排序原则的全部动机。

几个本讲用到的 torch 基础术语：

- **`TORCH_CHECK`**：torch 的断言宏，条件不满足时抛 `c10::Error`（Python 侧表现为 `RuntimeError`），消息由参数拼接。
- **`data_ptr()` 与 `nbytes()`**：张量第一个元素的设备地址与张量占用的字节数。视图的 `data_ptr` 可能落在存储中段。
- **storage 引用计数**：`tensor.storage().use_count() == 1` 表示除了登记表自己，没有别的持有者——`prune_owned()` 与 lend 槽检查都以此为「调用者是否已释放」的判据。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/bindings.cc` | 本讲主战场：`Call` 结构、`prepare()`、`transfer_on_stream()`、`copy_out()`、`run()`，以及作为「带事件镜像」的 `all_to_all_4d_timed`。 |
| `src/group.cc` | 校验链调用的 group 侧实现：`check_alive` / `prune_owned` / `plan`（内含 `make_dims` 检查）/ `window` / `lend_window` / `make_output` / `window_of`。 |
| `include/fast_ulysses/group.hpp` | `Window` 结构、`WindowRole`、`owned_` 表与 lend 池的契约注释。 |
| `include/fast_ulysses/transfer.hpp` | `launch_a2a_ce` 与 `fast_barrier` 的声明。 |
| `python/fast_ulysses/group.py` | Python 门面：部分校验（`out=` 与梯度、`out=` 与 `lend` 互斥）在 Python 侧先于 C++ 发生；`_timed` 给出四个阶段名。 |
| `test/distributed/validation.py` | 「每条文档化拒绝都 raise、且先于第一次握手」的测试工作负载，本讲多个实践的素材来源。 |
| `docs/api.md` | Raises 一节是校验链的对外契约文本。 |

## 4. 核心概念与源码讲解

### 4.1 prepare 校验链：一切检查先于第一道屏障

#### 4.1.1 概念说明

`prepare()` 是每次调用的「海关」：它把已验证的输入、目的地、要填充的窗口、以及「后两者是否同一缓冲」打包成一个 `Call` 结构返回。它不做任何 GPU 工作——不发射屏障、不发射拷贝。

排序原则只有一句话：**任何可能抛异常的检查，都必须发生在本次调用的第一道屏障之前**。原因在集合语义：如果 rank A 因非法参数在第 N 步抛出，而 rank B 已经通过了第 N 步、进入开路屏障（在 GPU 上自旋等待 A 发布标志），B 将永远等下去——一次「报错」退化成一次「挂起」。所以 api.md 把这句话写进了契约：

> `RuntimeError`, from validation that runs **before the call's first barrier**, so a rejected argument leaves no rank waiting on peers that did not reject it（[docs/api.md:L93-L96](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md#L93-L96)，Raises 一节的开头纲领）。

检查分两类：

- **per-call 检查**：依赖张量本身而非其形状——设备、连续性、与窗口的重叠。每次调用都要跑。
- **per-key 检查**：4D 形状、dtype 白名单、16 字节对齐、mode 取值、splits 合法性。它们在 `make_dims` 里，而 `make_dims` 只在 plan 缓存未命中时运行；由于 `PlanKey` 恰好包含全部被检值（sizes、mode、dtype、splits），缓存命中意味着同一组值已经通过过检查——缓存不会跳过任何会失败的检查（[src/group.cc:L39-L44](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L39-L44) 的注释明确论证了这一点）。

#### 4.1.2 核心流程

`prepare()`（被 `run()` 第一行调用）的完整执行顺序：

```text
Python 侧（group.py，先于一切 C++ 代码）
  0a. out= 且 grad 开启且 x.requires_grad  → raise（不可微拒绝）
  0b. out= 与 lend=True 同时给出          → raise（互斥拒绝）

op 入口（all_to_all_4d / all_to_all_4d_out）
  1. require_cuda(input)                   → CPU 张量在此拒绝，CUDAGuard 才能安全构造

prepare()
  2. check_alive()          → group 已 destroy 则拒绝
  3. prune_owned()          → 惰性回收调用者已释放的 empty_output 缓冲
  4. require_cuda(input)    → 冗余但必要：prepare 是所有路径的唯一收口
  5. require_group_device() → 输入设备必须等于 group 的设备
  6. x = input.contiguous() → 非连续输入在此隐式拷贝
  7. plan = group->plan(sizes, mode, dtype, splits…)
       └─ 缓存未命中时跑 make_dims：
          4D / dtype 白名单 / d*elem 16B 对齐 / mode∈{0,1}
          / splits 都传或都不传 / splits 与形状交叉校验 / 偶均分整除性
  8. 输出三分支（见 4.4）：
     8a. out= 有值   → 连续 CUDA / 设备 / dtype / 形状四查 → window_of 命中且字节够 → 零拷贝
     8b. lend=True  → lend_window 选槽，output = 槽的密集前缀视图
     8c. 都没有     → output = at::empty(output_shape)
  9. win 仍为空（8a 未命中或 8c）→ win = group->window(role, dtype, window_numel)
 10. 重叠检查：input 与窗口字节区间相交 → raise；
     若 out 不是窗口本身，out 与窗口相交 → raise
─── 到此为止没有任何 GPU 工作；此后才是屏障 → 传输 → 屏障 → copy_out ───
```

#### 4.1.3 源码精读

`Call` 结构：prepare 的全部产出就是它（[src/bindings.cc:L77-L85](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L77-L85)），注释说明它装的是「已验证的输入、目的地、传输要填充的窗口、以及后两者是否同一缓冲」：

```cpp
struct Call {
    at::Tensor     x;
    at::Tensor     output;
    const A2APlan* plan          = nullptr;
    const Window*  win           = nullptr;
    bool           out_is_window = false;
};
```

prepare 的开头与其排序理由（[src/bindings.cc:L87-L107](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L87-L107)）。第一段注释是全函数的纲领；第二段解释 `check_alive` 为什么放这里而不是更深处的 `window()` / `make_output()`——**零拷贝路径两者都到不了**，等更深的地方察觉时，开路屏障已经发射，那个 rank 就只能在等同伴：

```cpp
// Everything here runs BEFORE the call's first barrier, so a rejected argument leaves no rank
// waiting on peers that did not reject it.
Call prepare(...)
{
    Call call;
    // Here rather than deeper in: window() and make_output() also check, but the zero-copy path
    // reaches neither, and by the time anything else would notice the opening barrier has already
    // been issued -- so the rank that noticed would be waiting on peers instead of throwing.
    group->check_alive();
```

`check_alive` 本体只是一次 `TORCH_CHECK`（[src/group.cc:L162-L165](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L162-L165)）——`destroy()` 置位 `destroyed_` 后任何调用都被拒绝。

`prune_owned` 紧随其后、先于任何 `Window&` 的取出（[src/bindings.cc:L103-L106](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L103-L106) 与 [src/group.cc:L198-L205](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L198-L205)）：按 storage 引用计数等于 1（只剩登记表自己）删除 `owned_` 里的条目。放在这里的原因：本调用接下来可能从 `owned_` 取 `Window&`（零拷贝路径），先剪枝才不会让一个「调用者取走又丢掉」的缓冲一直被钉到 `destroy()`。

设备两项检查（[src/bindings.cc:L107-L110](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L107-L110)）：`require_cuda` 在任何 `CUDAGuard` 之前——注释（[src/bindings.cc:L28-L33](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L28-L33)）说明用 CPU 张量构造 guard 会在 c10 内部 abort，消息里既没有库名也没有参数名；`require_group_device`（[src/bindings.cc:L35-L48](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L35-L48)）拒绝其他 CUDA 设备上的张量，因为屏障内核会解引用窗口的 flag 指针、必须在正确的设备上启动。随后 `contiguous()` 与 `plan()`：

```cpp
    require_cuda(input);
    require_group_device(*group, input);
    call.x    = input.contiguous();
    call.plan = &group->plan(call.x.sizes(), mode, call.x.scalar_type(), seq_splits, head_splits);
```

`make_dims` 的全部 per-key 检查在 [src/group.cc:L45-L136](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L45-L136)（u2-l2 已逐条对照过消息文本，本讲只强调它属于链条第 7 步）。plan 的缓存与键在 [src/group.cc:L167-L196](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L167-L196)。

异步路径的例外：`run_staged` 把 `check_alive` / `require_cuda` / `require_group_device` 提前到 `stage()` **之前**单独执行（[src/bindings.cc:L374-L385](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L374-L385)），而不是全交给 prepare。注释给出两个理由：destroy 后再创建的 staging 条目永远无人释放、事件随 group 泄漏；staging 缓冲按「形状+dtype」缓存却继承输入的设备，一次错设备调用会留下一个错设备缓冲，毒化后续同形状调用。

Python 侧的两条前置检查（[python/fast_ulysses/group.py:L138-L143](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L138-L143) 拒绝 `out=` 带梯度输入、[python/fast_ulysses/group.py:L182-L187](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L182-L187) 拒绝 `out=` 与 `lend=True` 并用）——它们依赖 `torch.is_grad_enabled()` 这类只有 Python 侧看得到的上下文，所以校验链其实是跨语言的两级结构。

#### 4.1.4 代码实践

1. **实践目标**：把校验链的执行顺序固化成一张可对照的图，并把测试用例钉到具体检查上。
2. **操作步骤**：
   - 按 4.1.2 的伪代码画出你自己的顺序图（文本图即可），每个节点标注文件与行号，例如 `check_alive → src/group.cc:L162`。
   - 打开 [test/distributed/validation.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/validation.py#L1-L33)，通读开头 docstring：它声明了本测试的方法论——**每条拒绝都在每个 rank 上检查，随后各 rank 会合于一个 `dist.barrier()`**（[test/distributed/validation.py:L49-L64](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/validation.py#L49-L64) 的 `raises`：抛错后 `dist.barrier()`——如果某条拒绝不是全 rank 一致、或发生在开路握手之后，ranks 会在这里失步，worker 挂起而非失败，这正是要抓的失败模式）。
   - 做一张三列表格：`validation.py 用例 → 消息片段 → 源码检查点（文件:行）`。例如 `"a CPU input" → "must be a CUDA tensor" → bindings.cc require_cuda`；`"splits that contradict the shape" → "but the splits imply" → group.cc make_dims`。
3. **需要观察的现象**：表格中每条用例都能唯一映射到链条中的某一步，且没有任何一步位于「屏障」之后。
4. **预期结果**：映射完成后你会发现 validation.py 的用例顺序与本讲 4.1.2 的链条顺序几乎同构——测试作者就是按校验链组织的。
5. 本实践为源码阅读型，无需 GPU；如需实际运行 validation.py（单卡即可，`torchrun --nproc_per_node=1 test/distributed/validation.py`），现象与结论**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `check_alive` 必须放在 `prepare()` 里，而不是它调用的 `window()` / `make_output()` 里（这两个函数内部本来就有检查）？

**答案**：零拷贝路径（`out=` 来自 `empty_output()`）既不调用 `window()` 也不调用 `make_output()`——`win` 直接取自 `window_of()` 的命中。若检查只在那两处，一个已 destroy 的 group 会在零拷贝路径上跑完整个集合通信，甚至重建它刚拆掉的传输流（bindings.cc L99-L101 注释；validation.py L359-L383 的三个「destroyed group」用例覆盖了 copying、零拷贝、async 三条路径）。

**练习 2**：`make_dims` 里的 dtype 检查只在 plan 缓存未命中时运行。为什么这是安全的？

**答案**：`PlanKey` 包含 sizes、mode、dtype、seq/head splits——恰好是被检值的全体。命中缓存意味着完全相同的值组合曾经通过过检查；不同的值组合是不同的键，会重新走 `make_dims`（src/group.cc L39-L44 注释：缓存命中不可能跳过一个本会失败的检查）。

**练习 3**：`prune_owned()` 放在 prepare 的第 3 步而不是更早或更晚，各有理由。晚放的风险是什么？

**答案**：零拷贝路径会从 `owned_` 里取 `Window&`（第 8a 步）。如果剪枝发生在那之后，一个已被调用者释放（storage 引用计数归 1）的窗口仍留在表里被命中，对端会往一块内存可能已被分配器改派的钱上写。而「更早」（比如调用者释放时立即剪）做不到——C++ 侧看不到 Python 引用的消亡，只能在每个可观测时机惰性检查。

### 4.2 字节区间重叠检查：地址查找、按字节比、例外路径

#### 4.2.1 概念说明

窗口是**单缓冲**的：本次调用的 peer 拷贝会直接覆写窗口。如果输入 `x` 与窗口共享任何字节，传输过程就是「一边读、每个 peer 一边写同一块内存」——数据撕裂，且不报错。如果 `out` 与窗口**部分**重叠但不是窗口本身，那么 peers 写窗口、`copy_out` 再往 `out` 拷贝，两条写路径踩在同一块内存上，「既写不对也拷不对」。所以 prepare 的最后一组检查在发起任何 GPU 工作之前拒绝这两种调用。

这条检查有三个值得拆开的细节：

1. **地址查找**：零拷贝判定用 `window_of()`——拿 `out.data_ptr()` 去 `owned_` 表（键是 `const void*`，即窗口张量基址）做**精确匹配**。视图必须从窗口基址开始才会命中；从中间开始的切片命中不了，也就进不了零拷贝路径。
2. **按字节比**：相交判定用 `intervals_overlap`，比较的是 `[data_ptr, data_ptr + nbytes)` 与 `[窗口基址, 基址 + 窗口自身 nbytes)` 两个**字节区间**。窗口一侧用的是它自己的 nbytes（分配容量），不是本次调用所需的字节数。
3. **例外路径**：`out` **就是**窗口本身时（`out_is_window == true`）豁免 out 侧重叠检查——那是零拷贝路径，是唯一合法的「与窗口重叠」。输入一侧没有例外：输入永远不能是本次要填充的窗口。

#### 4.2.2 核心流程

两个闭区间（按字节）相交当且仅当：

\[ [a,\ a+n_a) \cap [b,\ b+n_b) \neq \varnothing \;\iff\; a < b + n_b \;\wedge\; b < a + n_a \]

实现正是这个谓词的直接翻译（严格不等式保证相邻区间——一方的终点等于另一方起点——不算重叠）。检查流程：

```text
base  = win->peer_ptrs[本rank]        # 窗口在本 rank 的本地地址
bytes = win->tensor.nbytes()          # 窗口自身的字节容量（高水位，非本次所需）

if [x.data_ptr, x.data_ptr + x.nbytes) 与 [base, base+bytes) 相交:
    raise "input overlaps the window this call fills ..."

if not out_is_window:                  # 例外路径：out 就是窗口 = 零拷贝，合法
    if [output.data_ptr, ... ) 与 [base, base+bytes) 相交:
        raise "out overlaps the window this call fills without being it ..."
```

#### 4.2.3 源码精读

区间谓词（[src/bindings.cc:L69-L75](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L69-L75)）——两个开区间相交的标准判定：

```cpp
// Byte intervals [a, a+a_bytes) and [b, b+b_bytes) intersect.
bool intervals_overlap(const void* a, int64_t a_bytes, const void* b, int64_t b_bytes)
{
    const auto* pa = static_cast<const char*>(a);
    const auto* pb = static_cast<const char*>(b);
    return pa < pb + b_bytes && pb < pa + a_bytes;
}
```

零拷贝判定：按地址命中 + 按字节比容量（[src/bindings.cc:L112-L139](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L112-L139)，重点是 L130-L138）。`out` 的四项检查（连续 CUDA、设备、dtype、形状）在前，然后：

```cpp
        // A buffer from empty_output() IS a window, and the peers write it directly. Compared in
        // BYTES: a window is found by address, so the dtype it was allocated for need not be this
        // call's, and comparing the two element counts would admit a buffer too small to hold what
        // the peers are about to write into it.
        const Window* owned = group->window_of(call.output);
        if (owned != nullptr && owned->tensor.nbytes() >= call.plan->window_numel * call.x.element_size()) {
            call.win           = owned;
            call.out_is_window = true;
        }
```

注意容量比较 `owned->tensor.nbytes() >= window_numel * element_size()` 两侧都换算成**字节**：左边是窗口分配时按它自己的 dtype 拿到的字节数，右边是本次调用按自己的 dtype 需要的字节数。若按元素个数比较，一个为 fp32 分配的窗口被 fp8 调用复用时，「元素够」可能掩盖「字节不够」。

`window_of` 本体（[src/group.cc:L314-L318](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L314-L318)）：`owned_` 以窗口张量基址为键的 `map::find`，精确地址匹配，不做范围查找：

```cpp
const Window* UlyssesGroup::window_of(const at::Tensor& out) const
{
    auto it = owned_.find(out.data_ptr());
    return it == owned_.end() ? nullptr : &it->second;
}
```

重叠检查本体（[src/bindings.cc:L158-L172](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L158-L172)）。窗口一侧取 `peer_ptrs[rank]`——即本 rank 窗口在本地的地址（`peer_ptrs` 是「从本 rank 视角看的各 rank 窗口地址」，第 rank 个就是自己的）；`bytes` 用窗口自身 nbytes。注释逐条对应三个细节：

```cpp
    // Refuse a call whose input, or whose `out`, shares bytes with the window it is about to fill
    // -- it would be read while every peer writes it. `out` BEING the window is the zero-copy path
    // and the one legitimate case. Intervals, not a pointer comparison, because an overlap can
    // start past the window's base. The window's OWN byte size, for the reason above.
    const void*   base  = reinterpret_cast<const void*>(call.win->peer_ptrs[group->rank()]);
    const int64_t bytes = call.win->tensor.nbytes();
    TORCH_CHECK(!intervals_overlap(call.x.data_ptr(), call.x.nbytes(), base, bytes),
                "input overlaps the window this call fills: ...");
    if (!call.out_is_window) {
        TORCH_CHECK(!intervals_overlap(call.output.data_ptr(), call.output.nbytes(), base, bytes),
                    "out overlaps the window this call fills without being it, ...");
    }
```

测试侧的权威表述：validation.py docstring 明确记录了「按窗口容量比而不是按本次所需比是保守形式，且公开 API 下只有下面一个用例可达」（[test/distributed/validation.py:L13-L17](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/validation.py#L13-L17)），并花大段解释 prepare 里两个按字节比较的守卫为何从公开 API **不可达**（形状/dtype 检查先拦住、跨 dtype 视图构造不出来、构造出来也是 rank 相关的会挂起）——「它们是给未来的调用者准备的守卫，不是给已存在的调用者的」（[test/distributed/validation.py:L19-L32](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/validation.py#L19-L32)）。可达的唯一用例（[test/distributed/validation.py:L163-L173](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/validation.py#L163-L173)）：把 `empty_output()` 的缓冲 view 成输入形状再喂回去——注释说明这个 reshape 在**任意 world size** 下都可行，而直接把缓冲原样喂回只在 ws==1 时形状巧合。

#### 4.2.4 代码实践

1. **实践目标**：亲手触发一次 `input overlaps the window`，并论证「按字节区间」与「按窗口自身 nbytes」两个设计选择。
2. **操作步骤**（需 ≥1 GPU，单卡即可；以下为示例代码，待本地验证）：

   ```python
   # 示例代码：重叠拒绝的最小复现（对照 validation.py L163-L173）
   import torch, torch.distributed as dist
   from fast_ulysses import UlyssesGroup

   dist.init_process_group("nccl")
   torch.cuda.set_device(int(__import__("os").environ["LOCAL_RANK"]))
   ws = dist.get_world_size()
   dev = torch.device("cuda", torch.cuda.current_device())

   group = UlyssesGroup(require_nvlink=False)
   good = torch.randn(2, 16, 4 * ws, 128, dtype=torch.bfloat16, device=dev)

   window = group.empty_output(good, mode=0)            # (2, 16*ws, 4, 128) 的密集前缀视图
   # 把同一块对称内存 view 成「本调用的输入」：形状对、dtype 对、地址重叠
   bad_input = window.view(2, 16, 4 * ws, 128)
   try:
       group.all_to_all_4d(bad_input, mode=0, out=window)
   except RuntimeError as e:
       assert "input overlaps the window" in str(e)
       print("rejected:", str(e)[:80])
   ```

   然后**扩展一步**（自己设计）：把 `bad_input` 换成从窗口中段开始的切片，例如对某个 4D 视图取 `[1:]`（保持连续），确认仍被拒绝。
3. **需要观察的现象**：两次调用都在进入任何传输之前立刻抛出 `RuntimeError`，消息分别含 `input overlaps the window`；进程不挂起。
4. **预期结果**与两个论证题：
   - **为什么必须比较字节区间而不是指针？** 重叠可以从窗口基址**之后**开始：切片视图的 `data_ptr` 是 `base + 偏移`，指针相等判断 `data_ptr == base` 会漏掉它；区间判定 `[ptr, ptr+nbytes) ∩ [base, base+bytes)` 才能覆盖「从中间开始但伸进窗口」与「从窗口中间开始」两种形态。bindings.cc L158-L161 注释原话："an overlap can start past the window's base"。
   - **为什么按窗口自身 nbytes 而不是本次调用所需字数？** 两个理由。其一（源码注释的理由）：窗口是按**地址**找到的，它分配时的 dtype 未必是本次调用的 dtype，按「元素个数」比较会让一个字节不够的缓冲通过——所以零拷贝判定的容量条件两边都换算成字节。其二（validation.py docstring 的理由）：role 窗口按高水位只增不减，容量 ≥ 本次所需；用容量做区间端点是**保守**形式——宁可误拒一个「与窗口空闲尾部重叠」的调用，也不放过任何真重叠。
5. 若无 GPU，本实践退化为源码阅读：在 4.2.3 引用的三处代码旁写出两个论证的答案即可；运行结果**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`out` 是 `empty_output()` 缓冲的一个**中段切片**（不从基址开始，但字节上与窗口重叠）。会发生什么？

**答案**：`window_of` 按基址精确匹配，切不中 → `owned == nullptr` → 不是零拷贝；接着 out 侧重叠检查发现 `output` 的字节区间与窗口相交且 `out_is_window == false` → 抛 `out overlaps the window this call fills without being it`。注意它**不会**走到「普通张量拷入」路径——重叠检查在路径分流之后、屏障之前兜底。

**练习 2**：输入侧重叠检查没有 `out_is_window` 式的豁免。为什么输入永远不能豁免？

**答案**：`out` 与窗口是同一缓冲时，写窗口=写结果，语义自洽（零拷贝）。而输入的语义是「被读」：传输期间每个 peer 都在往窗口写，任何与窗口重叠的输入都会被并发写坏——不存在「输入恰好该被写」的合法场景。唯一与窗口相关的合法输入是**上一次**调用的结果，而那由下一调用的开路屏障负责排序，不需要也不允许别名。

**练习 3**：把窗口一侧的字节数换成「本次所需」会怎样？给出一个具体失败场景。

**答案**：窗口按高水位增长：第一次调用每 rank 收 4 MB，窗口 4 MB；之后同 dtype 的一次小调用每 rank 只收 1 MB。若区间端点用「本次所需 1 MB」，一个从 `base + 1.5MB` 开始、长 1 MB 的输入与窗口空闲尾部重叠却通过检查——而 peers 的写入偏移由 plan 决定，本次确实只写前 1 MB，看似安全；但窗口是**按 (role,dtype) 共享**的，下一次 4 MB 调用会写满整窗，调用者若仍持有那个输入视图就会被写坏。保守形式切断这类跨调用的隐患（同时见 validation.py L13-L17：这一回归没有测试能抓，所以「别动容量形式」是写进 docstring 的告诫）。

### 4.3 transfer_on_stream：屏障 → 传输 → 屏障

#### 4.3.1 概念说明

`prepare` 返回后，`run()` 调用 `transfer_on_stream()`：本次调用对窗口做的一切，按顺序排到给定流上。它是三段式：

1. **开路屏障**——写者等读者。窗口单缓冲，本次调用即将覆写上一次的产物，而某个 peer 可能还在读它（比如上一次是零拷贝，调用者还握着结果）。屏障保证所有 rank 上的**读者**都过去了，才开始写。
2. **CE 传输**——`launch_a2a_ce` 按 plan 的 CopyOp 表发射 peer 拷贝（串行在组级 xfer 流上），本 rank 自份额走调用者流（u5-l1）。
3. **收尾屏障**——证明所有人的**写**已落地。它只约束写，不约束读；上一调用的读者何时读完是下一调用开路屏障的事。

一个必须诚实交代的假设：**「已完成的 CE 拷贝在随后内核的 release store 到达时对目的地可见」是假设，不是任何厂商文档保证的性质**。`test/distributed/ce_ordering.py` 是它的负控制（u9-l3 精读）。

#### 4.3.2 核心流程

同一条流上的时间线（`stream` 是调用者流；peer 拷贝在 xfer 流上、由事件汇回，见 u5-l1）：

```text
stream:   [开路 fast_barrier] ──→ [launch_a2a_ce: peer 拷贝×n + 自份额] ──→ [收尾 fast_barrier]
            写者等读者               拷贝引擎执行，零 SM                    证明写已落地
                                     （自份额与后段并行，同流排序）
```

为什么开路屏障挂在**本次调用的开头**而不是上一次调用的结尾？因为零拷贝路径的结果由**调用者**读取，读取发生在算子永远看不见的时刻——算子没有机会在自己的结尾上等读者读完。

#### 4.3.3 源码精读

`transfer_on_stream` 全文（[src/bindings.cc:L193-L211](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L193-L211)）。三行代码，三段注释各自承载一个论证：

```cpp
void transfer_on_stream(const c10::intrusive_ptr<UlyssesGroup>& group, const Call& call, cudaStream_t stream)
{
    const int rank = static_cast<int>(group->rank());

    // WRITERS WAIT FOR READERS, before writing anything. ... It guards the START of a call rather
    // than the end of the previous one because on the zero-copy path the result is read by the
    // caller at a time the operator never sees.
    fast_barrier(stream, call.win->flag_ptrs, rank);

    launch_a2a_ce(call.x.data_ptr(), call.win->peer_ptrs, *call.plan, group->xfer_stream(), rank, stream);

    // That a completed peer memcpy is VISIBLE at the destination when this kernel's release store
    // arrives is an ASSUMPTION, not a documented guarantee -- test/distributed/ce_ordering.py is
    // the negative control for it.
    fast_barrier(stream, call.win->flag_ptrs, rank);
}
```

`fast_barrier` 的声明（[include/fast_ulysses/transfer.hpp:L48-L51](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/transfer.hpp#L48-L51)）：在一给定流上、用 `flag_ptrs`（各 rank 的握手区）做 `rank` 的握手——内核实现在 src/barrier.cu（u5-l3 已逐行精读）。`launch_a2a_ce` 的参数表（[include/fast_ulysses/transfer.hpp:L27-L33](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/transfer.hpp#L27-L33)）：源地址、各 peer 窗口地址表、plan、组级 xfer 流、本 rank 序号、调用者流。

这条三段式的「带事件镜像」是 `all_to_all_4d_timed`（[src/bindings.cc:L624-L656](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L624-L656)）：同样先 `prepare`，然后在同样三段之间插 5 个 CUDA 事件（[src/bindings.cc:L637-L646](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L637-L646)），四个相邻差就是四个阶段——Python 侧的名字在 [python/fast_ulysses/group.py:L264-L265](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L264-L265)：`("barrier_in", "transfer", "barrier_out", "copy_out")`。注释还交代了一个测量边界：`transfer` 段含本 rank 自份额（它在调用者流上、与 peer 拷贝并行，无法单独计时）。

#### 4.3.4 代码实践

1. **实践目标**：用 `_timed` 把三段式拆成可见的四个数字，并读懂每段回答什么问题。
2. **操作步骤**（需 ≥1 GPU，单卡即可；示例代码，待本地验证）：

   ```python
   # 示例代码：阶段分解
   x = torch.randn(1, 4096, 40, 384, dtype=torch.bfloat16, device=dev)
   group.all_to_all_4d(x, mode=0)                 # 预热：先触发窗口与 plan
   out, stages = group._timed(x, mode=0)
   for name, ms in stages.items():
       print(f"{name:>10}: {ms:.3f} ms")
   ```

   阅读对照：`all_to_all_4d_timed` 源码（L637-L646）里 5 个事件与三段式的对应——`marks[0→1]` 是 barrier_in，`marks[1→2]` 是 transfer，`marks[2→3]` 是 barrier_out，`marks[3→4]` 是 copy_out。
3. **需要观察的现象**：四个数字之和 ≈ 整次调用的墙钟（同一流上严格排序，注释 L622-L623 说明这正是模式成立的前提）；ws=1 时两段 barrier 接近 0（u1-l3：world_size=1 整体跳过屏障），transfer 仍非零（自份额仍走窗口，见 4.4）。
4. **预期结果**：`barrier_in`/`barrier_out` 是与消息大小弱相关的固定开销（两道握手延迟），`transfer` 随字节量增长。这与 u1-l1「每次调用付两道屏障」的成本模型一致。具体数值**待本地验证**。
5. 无 GPU 时的替代实践：仅做源码阅读——在 L637-L646 的每个 `cudaEventRecord` 旁标注它切分的是三段式中的哪一段，并写出「为什么 barrier 必须在调用开头而非上一次调用结尾」的一句话答案（4.3.1 已给）。

#### 4.3.5 小练习与答案

**练习 1**：开路屏障和收尾屏障分别保护什么？为什么需要两道而不是一道？

**答案**：开路屏障=写者等**读者**（防覆写正在被读的上一产物）；收尾屏障=证明**写**已落地（让下一调用的读者看到完整数据）。二者不可合并：上一调用结尾的屏障无法保护零拷贝结果——它被调用者读取的时刻在算子看不见的未来；而「写完成」的证明又必须发生在写之后。单缓冲窗口因此天然需要「读写两个方向各一道」。

**练习 2**：`transfer_on_stream` 里传输的寻址参数只有 `call.x.data_ptr()`、`call.win->peer_ptrs`、`*call.plan` 三个。偏移计算在哪里完成？

**答案**：全部在 plan 里。`launch_a2a_ce` 不算任何偏移，逐条执行 `plan->ops` 的 CopyOp（src_offset/dst_offset 相对基址、pitch、width、rows，u3-l1）。这正是「寻址纯主机层」与「传输层」的分工：本函数只是把「基址 + 偏移表」交给拷贝引擎。

**练习 3**：如果删掉收尾屏障，哪类错误会在什么时刻暴露？

**答案**：本 rank 的 copy_out（同一条流上、排在屏障之后）可能拷到尚未写完的窗口字节，或零拷贝路径上调用者直接读到撕裂数据；peer 侧同理——下一 rank 的开路屏障只保证「上次的读者读完了」，不保证「这次的写写完了」。错误不是确定性崩溃而是偶发撕裂，最难排查；这正是 u9-l3 里对抗性测试（ce_ordering 的负控制、window_race）要逼出来的类别。

### 4.4 copy_out 与它的消失：三条路径的 buffer 来源

#### 4.4.1 概念说明

`copy_out` 把窗口内容搬到调用者最终拿到的张量里。它存在的前提是 u3-l2 买下的不变量：**每 rank 的结果从窗口基址起稠密排列**（本 rank 自份额也走窗口），所以窗口 → 目的地是**一条平坦的 D2D 拷贝**，没有任何交织内核。

它是否执行由 `out_is_window` 一个布尔决定——这个布尔在 prepare 的三分支里被赋值，于是「三条路径」其实是 prepare 第 8 步的三个出口加上 run 最后一个 if 的组合：

| 路径 | `output` 来源 | `win` 来源 | `out_is_window` | `copy_out` |
| --- | --- | --- | --- | --- |
| **copying**（默认） | `at::empty(plan->output_shape)`，普通 caching allocator | `group->window(role, dtype, window_numel)`——组内 (role,dtype) 缓存窗口 | false | **有**：窗口 → 新张量 |
| **copying**（普通 `out=`） | 调用者的任意连续 CUDA 张量（形状/dtype 已查） | 同上（`window_of` 未命中） | false | **有**：窗口 → 调用者张量 |
| **zero-copy**（`out=` 来自 `empty_output()`） | 调用者的窗口缓冲（`owned_` 命中且字节够） | 就是这个缓冲本身 | true | **无**：peers 直写 |
| **lend**（仅 async） | 出借窗口的密集前缀视图 | `lend_window()` 轮转选出的槽 | true | **无**：peers 直写 |

copying 与 zero-copy/lend 的差别就是 u5-l2 讲过的那台同设备拷贝：`out=`（或 `lend=True`）消除 copy-out，是端到端零 SM 的必要条件之一。

#### 4.4.2 核心流程

`run()` 的骨架（[src/bindings.cc:L225-L241](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L225-L241)）：

```text
call = prepare(...)                    # 三分支在这里分流，定好 output / win / out_is_window
transfer_on_stream(group, call, stream)  # 屏障 → CE 传输 → 屏障
if not call.out_is_window:
    copy_out(call, stream, rank)       # 窗口 → output，一条平坦 D2D 拷贝
return call.output                     # zero-copy/lend：这就是窗口（或其视图）本身
```

#### 4.4.3 源码精读

prepare 的输出三分支（[src/bindings.cc:L112-L156](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L112-L156)）。`out` 分支先做四项检查再探零拷贝（4.2.3 已引 L130-L138）；`lend` 分支取轮转窗口并造密集前缀视图：

```cpp
    else if (lend) {
        // No `out`, but the caller accepts a lent window: ... Which window is a rotation the
        // group drives, not a choice this call makes -- see lend_window().
        const Window& lent = group->lend_window(call.x.scalar_type(), call.plan->window_numel);
        call.win           = &lent;
        call.out_is_window = true;
        call.output =
            lent.tensor.narrow(0, 0, c10::multiply_integers(call.plan->output_shape)).view(call.plan->output_shape);
    }
    else {
        call.output = at::empty(call.plan->output_shape, call.x.options());
    }

    if (call.win == nullptr) {                       // 默认路径、或 out= 未命中零拷贝
        call.win = &group->window(role, call.x.scalar_type(), call.plan->window_numel);
    }
```

`copy_out` 全文（[src/bindings.cc:L213-L223](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L213-L223)）：从本 rank 窗口基址（`peer_ptrs[rank]`）到 `output`，按 `output.nbytes()` 一次 `cudaMemcpyAsync`，排在同一条流上——天然位于收尾屏障之后：

```cpp
// Window -> the caller's tensor, ordered after the closing barrier on the same stream. Every
// rank's result is dense from the window base, so this is one flat device-to-device copy -- this
// rank's own share included, since it travels through the window like every peer's.
void copy_out(const Call& call, cudaStream_t stream, int rank)
{
    ULYSSES_CUDA_CHECK(cudaMemcpyAsync(call.output.data_ptr(),
                                       reinterpret_cast<const void*>(call.win->peer_ptrs[rank]),
                                       static_cast<size_t>(call.output.nbytes()),
                                       cudaMemcpyDeviceToDevice,
                                       stream));
}
```

`run()` 与条件执行（[src/bindings.cc:L225-L241](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L225-L241)）：

```cpp
    const Call call = prepare(group, input, mode, seq_splits, head_splits, out, role, lend);
    transfer_on_stream(group, call, stream);
    if (!call.out_is_window) {
        copy_out(call, stream, static_cast<int>(group->rank()));
    }
    return call.output;
```

三条路径的 group 侧供给：缓存窗口的按需增长在 [src/group.cc:L255-L267](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L255-L267)（容量够即复用、不足先 erase 再 allocate）；lend 轮转在 [src/group.cc:L269-L302](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L269-L302)（`idx = pool.calls++ % kLentSlots`，引用计数只做检查，u4-l4）；`empty_output` 登记窗口进 `owned_` 并返回前缀视图在 [src/group.cc:L304-L312](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L304-L312)。

一个容易忽略的推论：**普通 `out=` 依然是 copying 路径**。`out=` 只有在 `window_of` 命中（即缓冲来自 `empty_output()`）时才免拷贝；一个形状正确的普通 CUDA 张量会被四项检查放行，然后照常从窗口拷入——api.md 里「`out` from `empty_output()` skips the copy-out; any other contiguous CUDA tensor of the output shape is copied into」说的就是这三分支。

#### 4.4.4 代码实践

1. **实践目标**：对同一输入分别走三条路径，验证 `copy_out` 阶段的存在与消失。
2. **操作步骤**（需 ≥1 GPU；单卡可验证 copying 与 zero-copy，lend 需 async 入口同样单卡可用。示例代码，待本地验证）：

   ```python
   # 示例代码：copy_out 的有与无
   x = torch.randn(1, 4096, 40, 384, dtype=torch.bfloat16, device=dev)  # b=1, s_local=4096, n=40, d=384
   group.all_to_all_4d(x, mode=0)                       # 预热

   # 路径 A：copying（默认）——copy_out 存在
   _, st = group._timed(x, mode=0)
   print("default copy_out:", st["copy_out"], "ms")

   # 路径 B：zero-copy —— out= 来自 empty_output()
   buf = group.empty_output(x, mode=0)
   y = group.all_to_all_4d(x, mode=0, out=buf)
   assert y.data_ptr() == buf.data_ptr()                # 结果就是缓冲本身，没有拷出
   ```

   对路径 B 的计时没有 `_timed` 入口（timed 不收 `out=`），改用外层 CUDA 事件包住循环，与路径 A 同法对比；或直接比较 `st["copy_out"]` 与「A、B 两次外层计时之差」。
3. **需要观察的现象**：路径 A 的 stages 里 `copy_out > 0` 且随字节数缩放（同设备拷贝，u5-l2）；路径 B 返回值的 `data_ptr` 与缓冲相同（零拷贝），外层计时应少出约一个 copy_out 的量。路径 C（`group.all_to_all_4d_async(x, lend=True).wait()`）同样返回窗口视图。
4. **预期结果**：三条路径结果数值一致（可与 mode 0 + mode 1 往返对照）；时间差 ≈ copy_out 段。单卡（ws=1）下所有路径仍工作，仅 barrier 段为 0。具体数字**待本地验证**。
5. 无 GPU 时的纸面实践：照 4.4.1 的表，对每行写出 `output` 与 `win` 两个字段的来源代码行号，并回答——普通 `out=` 为什么不能免拷贝？（它的缓冲不在对称内存里，peers 写不到它。）

#### 4.4.5 小练习与答案

**练习 1**：`copy_out` 为什么可以是一条平坦拷贝，而不需要任何重排？

**答案**：u3-l2 的设计决定：本 rank 自份额也经窗口写入，且 plan 把每个 peer 的贡献落在窗口内正确的稠密前缀位置。于是窗口从基址起就是输出的精确字节序列，`copy_out` 只需 `memcpy(nbytes)`；对照基线需要 `permute + contiguous` 重排内核（u1-l1：约占 BASE 开销 47–60% 的那部分在此不存在）。

**练习 2**：`run()` 里 `copy_out` 的条件是 `!call.out_is_window`，而不是「调用者传了 `out=`」。这两个判据等价吗？

**答案**：不等价，且差一个关键情形：调用者传了 `out=` 但缓冲**不是**窗口（普通张量），`out_is_window` 为 false，照样要 copy_out（拷入调用者张量）；反过来 `out_is_window` 为 true 的另一条路是 `lend=True`（没有 `out=`）。用「是否传了 out=」做判据会漏掉这两种。正确的判据是「目的地与传输目的地是否同一缓冲」。

**练习 3**：lend 路径的 `call.output` 是 `lent.tensor.narrow(0, 0, N).view(shape)`。为什么要 `narrow` 再 `view`，而不是直接用窗口张量？

**答案**：不均匀分片下窗口按全组最大接收量开出（`window_numel` 取 max，u3-l1），可能大于本 rank 输出的元素数。`narrow(0, 0, N)` 取密集前缀 N 个元素、`view` 整形成本 rank 的输出形状——与 `make_output()` 的做法一字不差（src/group.cc L309）。直接返回窗口张量会把别的 rank 的「空位」也暴露给调用者，形状就错了。

## 5. 综合实践

**任务：给一次调用做「全流水线体检」——顺序图 + 阶段分解 + 拒绝路径，三者互相印证。**

写一个脚本（示例代码，需 ≥1 GPU，单卡即可；多卡则用 `torchrun --nproc_per_node=N` 启动），依次完成：

1. **顺序图**：不看本讲，凭源码重画 4.1.2 的校验链顺序图（含 Python 侧两级检查），每个节点标 `文件:行`。画完与本讲对照，补上遗漏。
2. **阶段分解**：预热后调用 `group._timed(x, mode=0)`，记录四个阶段数字；再放大 `x` 的序列长度一倍，记录第二次。对照回答：哪些段基本不变（屏障 ×2），哪些段近似翻倍（transfer、copy_out）？
3. **零拷贝对照**：`buf = group.empty_output(x, mode=0)` 后用外层 CUDA 事件分别包住「默认调用」与 `out=buf` 调用各 20 次的循环，验证两者差值与 `st["copy_out"]` 同量级，且 `y.data_ptr() == buf.data_ptr()`。
4. **拒绝路径**：执行 4.2.4 的重叠用例，确认立即抛错、不挂起；再执行 `validation.py`（`torchrun --nproc_per_node=1 test/distributed/validation.py`），把输出里的每一行 `OK ws=1 rejects ...` 对到顺序图的一个节点上。

预期产出：一张顺序图、一份两行四列的阶段数字表、一条 `data_ptr` 相等断言的通过记录、一张 validation 用例 → 检查点的映射表。若任何一步现象与预期不符（例如 `out=` 没有省出 copy_out 的时间），回到 4.4.1 的表格检查自己走的是哪条路径。全部运行结果**待本地验证**。

## 6. 本讲小结

- `prepare()` 是纯主机侧的海关，产出 `Call{x, output, plan, win, out_is_window}`；**一切检查先于第一道屏障**，否则单 rank 抛错会把同伴留在 GPU 自旋里——「报错」退化成「挂起」。
- 校验链两级跨语言：Python 侧先拒（`out=` 带梯度、`out=`+`lend` 互斥），C++ 侧按 `check_alive → prune_owned → require_cuda → 设备 → contiguous → plan(make_dims 全套) → 输出三分支 → 窗口兜底 → 重叠检查` 推进；`check_alive` 必须在 prepare 因为零拷贝路径不经过 `window()`/`make_output()`。
- 重叠检查三细节：`window_of` 按基址**精确匹配** `owned_`；相交按**字节区间**判定（重叠可始于窗口中段，指针相等会漏）；窗口一侧用**自身 nbytes**（容量，保守），且 `out` 就是窗口时豁免——那是零拷贝的唯一合法重叠。
- `transfer_on_stream` 三段式：开路屏障（写者等读者，挂在调用开头因为零拷贝结果的读取时刻算子看不见）→ CE 传输（寻址全在 plan）→ 收尾屏障（证明写落地）；「CE 写可见性」是假设，ce_ordering 是其负控制。
- `copy_out` 是一条平坦 D2D 拷贝（窗口稠密前缀不变量），只在 `out_is_window == false` 时执行；三条路径的差别全在 buffer 来源：默认 `at::empty`、普通 `out=` 调用者张量（仍拷贝）、zero-copy/lend 窗口本身（peers 直写，拷贝消失）。
- `all_to_all_4d_timed` 是这条流水线的带事件镜像，把四阶段（barrier_in / transfer / barrier_out / copy_out）拆成可测数字，是基准方法论（u9-l4）的探针。

## 7. 下一步学习建议

- **下一讲 u6-l3《自动微分：backward 是另一个 mode》**：本讲的 `run()` 之上还包着一层 `A2AFunction`——`dispatch_below_autograd` 如何带着本讲的 prepare 流水线重入调度器、backward 为什么恰好是 `1 - mode` 且 splits 不交换，是同一个文件里紧接着的下一块硬骨。
- 若你更关心异步路径：u7-l1 将从 `run_staged`（本讲 4.1.3 的末尾已露面）展开 comm 流、staging 与 `AsyncCollectiveTensor`；`StagingRelease`（bindings.cc L176-L191）的异常安全释放会在 u7-l2 精读。
- 若想看本讲校验链的「全集测试」：直接通读 [test/distributed/validation.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/validation.py#L1-L33) 与 [test/distributed/correctness.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py)，u9-l2 会逐维度拆解它们各自证明了什么。
