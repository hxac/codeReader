# 无 GPU 的布局测试：plan_bindings 与 test_plan

## 1. 本讲目标

学完本讲，你应该能够：

1. 说明 `a2a_plan_debug` 这个调试算子如何把纯主机的 plan 暴露给 Python，以及为什么它必须放在一个独立的翻译单元里。
2. 解释 `test/test_plan.py` 的「重放测试」如何把**布局正确性**与**传输正确性**解耦：如果重放通过，之后在 GPU 上出的问题一定是传输或同步 bug，而不是寻址 bug。
3. 写出 `reference_scatter_head` / `reference_gather_head` 的语义（它们是两个 all-to-all 模式的 numpy 权威表述），并说明 `test_matches_torch_all_to_all_single` 如何把这份自述的参照锚定回 torch 本身。
4. 解释零长度 shard（`seq_splits` 里出现 0）与非 2 幂 world_size 各自覆盖 `a2a_plan.cc` 和 `transfer.cu` 里的哪些代码分支。
5. 亲手为 `test_plan.py` 增加一个 ws=5 且含零长度 shard 的新测试用例。

## 2. 前置知识

本讲默认你已读过 u3-l1（A2ADims / CopyOp / A2APlan 三个数据结构）与 u3-l2（`build_plan` 的两种模式发射）。快速回顾几个会反复用到的结论：

- **CopyOp 是无指针的**：它用 10 个整数（peer、src/dst offset、src/dst pitch、width、rows、depth、src/dst slice）描述一条「相对缓冲区基址」的带步长字节拷贝。因为它不含指针，同一份描述既能在 CUDA 侧变成 `cudaMemcpy2D/3DAsync`，也能在主机侧用 numpy 按字节重放——这是本讲全部内容的基石。
- **window_numel 取全组最大**：窗口必须装下「最大的那个 rank 的输出」，且所有 rank 的窗口尺寸必须相同，peer 的写入偏移才能对上。
- **本 rank 份额也走窗口**：`ops` 覆盖包括自己在内的全部 ws 个目的地。

本讲还需要几个测试工程里的常用概念：

- **参照（reference / oracle）**：一份独立于被测代码实现的、语义上「大家都同意」的期望结果。本讲的参照就是 `torch.distributed.all_to_all_single` + permute——这是每一个 Ulysses 实现都对齐的语义。
- **重放（replay）**：不真正跨 GPU 通信，而是在主机上用普通字节缓冲模拟执行。给每个「rank」发一个窗口缓冲，然后逐条执行 CopyOp 的拷贝语义。
- **位精确（bit-exact）**：本讲的对照用 `np.testing.assert_array_equal`（逐字节相等），而不是 u2-l4 里 DiT 示例用的 `allclose`。原因是这里对照的两侧都是**纯数据搬运**，没有任何浮点归约，逐位相等是应有的性质。
- **负控制（negative control）**：故意破坏被测代码的一个保护，确认测试真的会失败——防止「测试变盲」。`test_plan.py` 文件末尾的溢出用例带有一段醒目的负控制注释，u9-l3 会展开这个主题。
- **`pytest.importorskip`**：导入失败时把测试标记为 skip 而不是 error。`test_plan.py` 用它声明「本测试只需要编好的 `_C` 扩展，不需要 GPU、不需要进程组」。

一个值得先记住的分层事实：`a2a_plan.cc` **本身不关心 world_size 是不是 2 的幂**——它只是 `for (p = 0; p < world_size; ++p)` 地枚举目的地。「2 的幂」只对 `transfer.cu` 里的 XOR peer 枚举有意义。本讲 4.4 节会精确地划这条线。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/plan_bindings.cc](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/plan_bindings.cc) | 唯一让 torch 与纯主机 plan 相遇的翻译单元；注册 `a2a_plan_debug` 算子 |
| [test/test_plan.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py) | 无 GPU 的布局契约测试：numpy 重放 + 参照对照 |
| [src/a2a_plan.cc](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc) | 被测的纯主机寻址实现（u3-l1/u3-l2 已精读，本讲只引用其中两个跳过守卫） |
| [src/transfer.cu](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/transfer.cu) | `launch_a2a_ce` 的 XOR 枚举与非 2 幂 sweep 分支（本讲只引用 peer 枚举段） |
| [docs/develop.md](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/develop.md) | 开发文档：测试分层、CI 能证明什么 |
| [.github/workflows/ci.yml](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/.github/workflows/ci.yml) | CI 在无 GPU runner 上运行 `test_plan.py` 的证据 |

## 4. 核心概念与源码讲解

### 4.1 a2a_plan_debug 绑定：plan 与 torch 的唯一相遇点

#### 4.1.1 概念说明

u1-l4 建立过一条分层纪律：`a2a_plan.cc` 里没有 CUDA、没有 torch、没有通信库。好处是布局逻辑可以脱离 GPU 测试——但 Python 测试要调用它，中间必须有一座桥。`src/plan_bindings.cc` 就是这座桥，而且**全仓库只有这一处** torch 代码与 plan 代码相遇。

为什么桥要单独占一个翻译单元（TU）？文件头注释直接回答了：

> [src/plan_bindings.cc:L1-L4](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/plan_bindings.cc#L1-L4) —— 文件头注释说明：这个文件把纯主机 plan 暴露给 Python，让 test_plan.py 能在无 GPU 机器上用 numpy 缓冲重放它；`a2a_plan.cc` 本体保持无 torch 无 CUDA，本文件是两者唯一相遇处。与 `bindings.cc` 的算子 schema 不同，这里的 schema **不引用任何自定义类**，所以静态初始化顺序无关紧要，单独一个 TU 是安全的。

回顾 u6-l1 会展开的背景：`bindings.cc` 里注册的算子 schema 引用了 `UlyssesGroup` 这个 torchbind 自定义类，跨 TU 的静态初始化顺序会成为问题；`a2a_plan_debug` 的参数和返回值全是基本类型（`int`、`int[]`、`Tensor`），没有这层顾虑。

#### 4.1.2 核心流程

`a2a_plan_debug` 做的事可以概括为「装填 → 构建 → 摊平 → 返回」：

```text
输入: b, d, rank, world_size, seq_splits, head_splits, mode, elem_size
  │
  ├─ 1. 装填 A2ADims（纯数据结构，无任何 torch 类型）
  ├─ 2. build_plan(dims, mode, elem_size) —— 与 GPU 路径调用的是同一个函数
  ├─ 3. 把 plan.ops 摊平成 (N × 10) 的 int64 张量，一行一个 CopyOp
  └─ 4. 返回 (output_shape: int[], window_numel: int, ops: Tensor)
```

关键点：GPU 上真实传输用的也是 `build_plan` 的返回值。所以测试拿到的 ops 表**就是**设备要执行的拷贝清单——不是另一份平行实现。这就是「重放有代表性」的保证。

#### 4.1.3 源码精读

装填与构建：

- [src/plan_bindings.cc:L26-L34](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/plan_bindings.cc#L26-L34) —— 把 Python 传来的整数参数装进 `A2ADims`，然后调用 `build_plan(dims, mode, elem_size)`。注意 `seq_splits`/`head_splits` 用 `std::move` 移入，`rank`/`world_size` 从 int64 收窄为 int。

摊平成张量：

- [src/plan_bindings.cc:L36-L50](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/plan_bindings.cc#L36-L50) —— 分配 `(ops.size(), 10)` 的 int64 张量，逐行写入 CopyOp 的十个字段。L14 的注释明确列出列序：`peer, src_off, dst_off, src_pitch, dst_pitch, width, rows, depth, src_slice, dst_slice`——这个列序是 test_plan.py 解包的契约。
- [src/plan_bindings.cc:L51](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/plan_bindings.cc#L51) —— 返回三元组 `(plan.output_shape, plan.window_numel, ops)`。

schema 注册：

- [src/plan_bindings.cc:L57-L62](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/plan_bindings.cc#L57-L62) —— 用 `TORCH_LIBRARY_FRAGMENT` 注册到 `fast_ulysses` 命名空间，schema 为 `a2a_plan_debug(int b, int d, int rank, int world_size, int[] seq_splits, int[] head_splits, int mode, int elem_size) -> (int[], int, Tensor)`。`FRAGMENT` 允许同一个命名空间分多个 TU 注册（`bindings.cc` 注册其余算子）。

Python 侧的包装在 [test/test_plan.py:L27-L37](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L27-L37)：调用算子、把 output_shape 转成 list、顺手算出 `output_bytes = prod(output_shape) * elem_size`（后面给重放分配窗口用），并把 ops 转成 numpy 数组。

还有一处工程细节值得注意：CI 是从 `/tmp` 目录用绝对路径运行这个测试的，见 [.github/workflows/ci.yml:L78-L93](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/.github/workflows/ci.yml#L78-L93)。注释解释了原因：checkout 出来的 `python/` 目录里那份同名包没有编好的 `_C`，而 `test_plan.py` 遇到导入失败会**安静地 skip**（[test/test_plan.py:L19-L21](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L19-L21) 的 `importorskip`）——如果导入了源码树里那份，「全部通过」其实等于「全部跳过」。

#### 4.1.4 代码实践

**实践目标**：第一次亲手调用 `a2a_plan_debug`，打印一张 ops 表，并与手工推演对照。

**操作步骤**（示例代码，需要在装好本扩展的环境运行；无 GPU 也可）：

```python
# 示例代码：打印一个 2-rank、不均分、mode 0 的 plan
import torch
print([str(s) for s in torch.ops.fast_ulysses.a2a_plan_debug.default._schemas])  # 可选：查看 schema

# b=1, d=4, ws=2, seq_splits=[2,1], head_splits=[1,2], mode 0, float32
out = torch.ops.fast_ulysses.a2a_plan_debug(
    1, 4, 0, 2, [2, 1], [1, 2], 0, 4
)
output_shape, window_numel, ops = out
print("output_shape =", list(output_shape))
print("window_numel =", int(window_numel))
print("columns: peer, src_off, dst_off, src_pitch, dst_pitch, width, rows, depth, src_slice, dst_slice")
print(ops.numpy())
```

**手工推演（预期结果）**：按 u3-l2 的公式，`seq_total=3`、`head_total=3`、`d_bytes=16`，rank 0 拥有 \( s_0 = 2 \)、\( H_0 = [0, 1] \)、\( S_0 = [0, 2] \)：

- 发给 peer 0（自己）：`row = head_splits[0]*d_bytes = 16`，`src_offset = H_0[0]*16 = 0`，`src_pitch = 3*16 = 48`，`dst_offset = S_0[0]*16 = 0`，`dst_pitch = 16`，`width = 16`，`rows = s_0 = 2`，`depth = 1`。
- 发给 peer 1：`row = 2*16 = 32`，`src_offset = H_0[1]*16 = 16`，`src_pitch = 48`，`dst_offset = S_0[0]*32 = 0`，`dst_pitch = 32`，`width = 32`，`rows = 2`，`depth = 1`。
- `output_shape = [1, 3, 1, 4]`（12 个元素），而 `window_numel = 1 × max(3·1·4, 3·2·4) = 24`——**不是本 rank 的 12**。

**需要观察的现象**：打印出的两行 ops 是否与上面推演逐字段一致；`window_numel` 是否等于 24（全组最大，来自 rank 1 的输出 \( 3 \times 2 \times 4 = 24 \)）。

**预期结果**：一致。若不一致，先检查你是否把 `src_pitch`（发送侧满头行宽 `head_total·d_bytes`）与 `dst_pitch`（接收侧分片行宽）记反——这是 mode 0「读跨步、写连续」的最常见混淆点。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `a2a_plan_debug` 不能干脆并进 `bindings.cc`，或者干脆在 Python 里用 numpy 重写一份 `build_plan`？

**答案**：并进 `bindings.cc` 会让「plan 保持无 torch」的纪律失守——头注释（L1-L4）说明本文件的 schema 不引用自定义类，所以独立 TU 无静态初始化顺序问题，这是把它与 `bindings.cc` 分开的许可条件而非障碍；真正要守住的是 `a2a_plan.cc` 本体不沾 torch。在 Python 里重写一份则更糟：测试将测的是「平行实现」，两份代码可以一起错，重放通过不再能证明 GPU 用的那份寻址正确。桥必须喂 GPU 同一个 `build_plan`。

**练习 2**：`ops` 张量为什么选 int64 而不是 int32？

**答案**：CopyOp 的全部字段在 C++ 侧就是 `int64_t`（offset、pitch 以**字节**计，u3-l1 讲过三层溢出防护正说明这些乘积会很大）。用 int32 会在大形状下静默截断，测试反而可能「通过」。

**练习 3**：`pytest.importorskip("fast_ulysses")` 失败时测试报告是绿的（skip）。这会不会让 CI 变成「什么都没测」？

**答案**：有这个风险，所以 CI 特意从 `/tmp` 安装 wheel 后用绝对路径跑测试（ci.yml L78-L93 的注释），保证 import 到的是刚构建并安装的 `_C`，而不是源码树里没有扩展的同名包。skip 只应发生在「真的没构建」的开发机上。

### 4.2 numpy 重放：apply_ops 与 run_plan

#### 4.2.1 概念说明

有了 ops 表，下一步是**执行**它。`test_plan.py` 的做法是在主机内存里模拟设备上的数据流：给每个「rank」分配一个字节缓冲当它的对称窗口，然后让 rank r 的每条 CopyOp 写进 `windows[op.peer]`。

这就是「布局与传输解耦」的落点。模块 docstring 把论证写得非常清楚：

> [test/test_plan.py:L1-L12](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L1-L12) —— plan 是一组带步长拷贝；这里在普通字节缓冲上执行它们（每个模拟 rank 一个窗口），并与 `all_to_all_single` + permute 构建的参照比较。「如果这里通过，寻址就是对的，之后的失败是传输或同步 bug，不是布局 bug。」L9-L11 还说明：窗口在这里装下**整个输出**，每个 rank 像对待任何 peer 一样把自己的份额送进窗口，所以零拷贝路径与拷贝路径都不引入重放需要额外建模的寻址。

换句话说，这个测试把 bug 的搜索空间切成了两半：重放通过 → 排除布局；剩下 GPU 上出的问题 → 一定在传输或同步（u5、u9-l3 的领域）。对 CI 没有 GPU 这件事，这是它能做出的最强正确性声明（docs/develop.md L82-L84 明说了 CI 证明的是编译、链接、加载与 `test_plan.py` 通过）。

#### 4.2.2 核心流程

`run_plan` 的模拟流程：

```text
windows[p] = zeros(plans[p].output_bytes)     # 每个模拟 rank 一个窗口
for src_rank, plan in enumerate(plans):
    src = inputs[src_rank] 摊平成 uint8 视图
    for op in plan["ops"]:
        apply_ops([op], src, windows[op.peer])   # 发往 op.peer 的窗口
return windows
```

`apply_ops` 对单条 CopyOp 的执行就是三重循环的逐行字节拷贝：

\[ \text{dst}[t : t + w] \leftarrow \text{src}[s : s + w],\quad s = \text{src\_off} + z \cdot \text{src\_slice} + r \cdot \text{src\_pitch},\quad t = \text{dst\_off} + z \cdot \text{dst\_slice} + r \cdot \text{dst\_pitch} \]

其中 \( z \in [0, \text{depth}) \)、\( r \in [0, \text{rows}) \)、\( w = \text{width} \)。

一个精确的对应关系：设备侧 `depth` 是被 `push_batched` 折叠进一条 `cudaMemcpy3DAsync` 的批维重复；重放里它展开成「按 `src_slice`/`dst_slice` 固定步长重复同一份 2D 拷贝」——这正是 cudaMemcpy3D 的语义。

#### 4.2.3 源码精读

- [test/test_plan.py:L40-L59](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L40-L59) —— `apply_ops`：按 10 字段解包一条 op，三重循环逐行拷贝。L53-L54 的注释点明 depth 的重放语义与 `cudaMemcpy3DAsync` 相同。
- [test/test_plan.py:L62-L69](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L62-L69) —— `run_plan`：每个模拟 rank 一个窗口；注意窗口尺寸用的是 `p["output_bytes"]`（**该 rank 自己的输出字节数**），不是 `window_numel`。这一点下面细说。
- [test/test_plan.py:L101-L118](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L101-L118) —— `make_inputs`：每个元素取互不相同的值，「任何放错位置的字节都会立刻显形」。L112-L116 的注释解释了单字节 dtype 的技巧：计数用 int64 再收窄，1 字节类型会回绕、值每 256 个元素重复——这是单字节类型能提供的最大区分度，恰好够抓「不是 256 整数倍元素」的错位。

窗口尺寸为什么敢用「本 rank 输出字节数」而不是 `window_numel`？真实系统里窗口是全组统一的最大尺寸，而这里给小 rank 的缓冲更小——[test/test_plan.py:L141-L144](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L141-L144) 的注释回答了：由于输出是「自窗口基址稠密排列」的（u3-l1 讲过的 `output_shape` 不变量），任何 op 都不该写到某 rank 输出之外；写越界会立刻在 numpy 上越界报错。所以小缓冲是一层**免费加严**的检查，而不是放松。

#### 4.2.4 代码实践

**实践目标**：验证「重放 faithfully 执行 CopyOp」不是一句空话——用 4.1.4 打印出的 ops 表，在纸面上走一遍其中一行的三重循环，再和 numpy 重放比对。

**操作步骤**（示例代码）：

```python
# 示例代码：单条 op 的手工重放 vs apply_ops
import numpy as np
import sys
sys.path.insert(0, "test")  # 让下面的导入找到 test_plan（或直接把两个函数复制过来）
from test_plan import apply_ops, build_plan

plan = build_plan(1, 4, 0, 2, [2, 1], [1, 2], 0, 4)
src = np.arange(3 * 3 * 4, dtype=np.float32).reshape(1, 3, 3, 4)  # rank 0 的输入
dst = np.zeros(plan["output_bytes"], dtype=np.uint8)
apply_ops(plan["ops"], src.reshape(-1).view(np.uint8), dst)
print(dst.view(np.float32).reshape(plan["output_shape"]))
```

**需要观察的现象**：输出是一个 `(1, 3, 1, 4)` 的张量。取 peer 1 那条 op（`src_off=16, dst_off=0, src_pitch=48, dst_pitch=32, width=32, rows=2`），手工算两行的源区间：第 0 行读 `src[16:48]`、第 1 行读 `src[64:96]`（=16+48 与 16+2×48-16 偏移的对应关系自己核一遍），写入 `dst[0:32]` 和 `dst[32:64]`。

**预期结果**：手工算出的 8 个 float32 值与打印输出的对应位置一致。若手算对不上，回到 u3-l2 的 offset 公式重推，不要怀疑 `apply_ops`——它只有 9 行，没有藏任何逻辑。

#### 4.2.5 小练习与答案

**练习 1**：重放用 `windows[op[0]]` 决定写入目标。如果 `build_plan` 把本 rank 自己的份额排除在 `ops` 之外（即 `p == rank` 时不发射），重放会怎么失败？

**答案**：不会「失败」——这正是阴险之处。窗口里本 rank 应占的区段保持全零，与参照一比就会不相等，测试会**报出不相等**；但如果参照也犯同样的错（自己实现自己对照），两层一起错。这解释了为什么参照必须独立于实现（4.3），以及为什么「本 rank 份额走窗口」是 `build_plan` 的刻意设计（hpp L62-L64 的注释）。

**练习 2**：为什么 `apply_ops` 不把 depth 折叠成 numpy 的切片操作，而老实展开成 `for z in range(depth)`？

**答案**：重放的价值在于**语义上逐字对应**设备执行的 `cudaMemcpy3DAsync`：3D 拷贝就是「同一份 2D 拷贝按 slice 步长重复」。展开写让重放与设备行为同构，任何在折叠/展开之间做手脚的 plan 变化都会被同等暴露。test_plan.py L53-L54 注释明说了这一点。此外 `test_fused_ops_survive_cuda_memcpy_3d`（L206-L233）还专门断言了折叠后的 op 必须仍是合法的 cudaMemcpy3D 参数（slice 整除 pitch 等），因为 `apply_ops` 直接用 `src_slice` 步进，**不会**自动抓出违反这些约束的 plan——它只重放，不验证可表达性。

**练习 3**：`run_plan` 里 `src = inputs[src_rank].reshape(-1).view(np.uint8)` 为什么必须先 `reshape(-1)` 再 `view`？

**答案**：`.view(np.uint8)` 要求内存连续的解释；输入本来就是 C 连续的（`make_inputs` 里由 `reshape` 产生），`reshape(-1)` 把它摊平成 1 字节可解释的一维流。CopyOp 的 offset 是「相对输入张量基址的字节偏移」，摊平视图正是这个坐标系。

### 4.3 all_to_all_single 参照：reference_scatter_head 与 reference_gather_head

#### 4.3.1 概念说明

重放产出「实际结果」，还需要「期望结果」。`test_plan.py` 提供了两个 numpy 函数，分别是 mode 0 / mode 1 的语义权威表述：

- **`reference_scatter_head`（mode 0）**：每个 rank r 持有 `[b, s_r, n_total, d]`；rank p 的输出是「把每个 r 的输入切下 p 拥有的那段头列，再沿序列轴拼接」：

\[ \text{out}_p = \operatorname{concat}_{r=0}^{P-1}\bigl( x_r[:,\, :,\, H_p{:}H_{p+1},\, :] \bigr) \quad \text{沿 axis=1（序列轴）} \]

得到 `[b, s_total, n_p, d]`。直觉：p 要凑齐**全部序列**，但只要**自己那几列头**；每个 r 贡献自己那段序列上 p 的头列。

- **`reference_gather_head`（mode 1）**：镜像。每个 rank r 持有 `[b, s_total, n_r, d]`；rank p 的输出是「把每个 r 的输入切下 p 拥有的那段序列行，再沿头轴拼接」：

\[ \text{out}_p = \operatorname{concat}_{r=0}^{P-1}\bigl( x_r[:,\, S_p{:}S_{p+1},\, :,\, :] \bigr) \quad \text{沿 axis=2（头轴）} \]

得到 `[b, s_p, n_total, d]`。直觉：p 只要**自己那段序列**，但要那段的**全部头**。

但这里有个测试方法论的问题：这两个函数是我们自己写的，凭什么信它们？`test_matches_torch_all_to_all_single` 就是防「自说自话」的锚——它在一个进程里复现 `all_to_all_single` 的分块语义（rank r 的第 j 块送到 rank j 的第 r 槽），套上与 sglang `usp.py` 相同的 permute，证明 numpy 参照与 torch 集合通信的语义逐位一致。参照链条因此是完整的：

```text
plan 重放结果 == numpy 参照（reference_*） == torch all_to_all_single + permute 语义
```

第二个等号由锚定测试保证，第一个等号由所有其余测试反复检查。任何 Ulysses 实现都对齐最右端，所以本库的布局与生态兼容。

#### 4.3.2 核心流程

锚定测试的流程（偶均分场景，这样才能用 `.chunk(world_size)` 复现等块语义）：

```text
full = randn(b, s_total, n_total, d)              # 一个进程持有全局张量
inputs = full 按序列轴切成 ws 份 → 每个 rank 的输入
sends = [x.permute(2,0,1,3).contiguous().flatten()  # 头轴换到 dim 0（被散射轴换到最前）
         for x in inputs]
chunks = [s.chunk(ws) for s in sends]              # 每 rank 等分成 ws 块
for p:  recv = cat([chunks[r][p] for r in range(ws)])   # all_to_all_single 语义：r 的第 p 块给 p
        y = recv.reshape(ws, h_local, b, s_local, d)
        torch_out[p] = y.permute(2,0,3,1,4).reshape(b, s_total, h_local, d)
最后：run_plan(inputs 的 numpy) 与 torch_out 逐位比较
```

#### 4.3.3 源码精读

- [test/test_plan.py:L72-L86](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L72-L86) —— `reference_scatter_head`：`head_off = cumsum([0] + head_splits)` 给出每个 rank 的头区间，对每个目标 rank p，沿 axis=1 拼接所有输入的头切片。docstring（L73-L78）写明语义并指出这「正是把被散射轴 permute 到 dim 0 之后 all_to_all_single 所做的事」。
- [test/test_plan.py:L89-L98](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L89-L98) —— `reference_gather_head`：镜像逻辑，`seq_off = cumsum([0] + seq_splits)`，沿 axis=2 拼接序列切片。
- [test/test_plan.py:L260-L296](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L260-L296) —— `test_matches_torch_all_to_all_single`：docstring（L261-L266）说明目的是「把参照绑回 torch 本身，而不只是我们自己的 numpy 重述」。L275-L276 的注释指出 permute 序列取自 sglang 的 `_usp_input_all_to_all`。
- 对照发生处：`check()` 的 [test/test_plan.py:L131-L139](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L131-L139) —— 按模式选参照，`assert_array_equal` 逐位比较，形状先断言（`got.shape == expected[r].shape`）。

一个细节：锚定测试只测偶均分。不均分场景没有等块的 `all_to_all_single` 语义可直接复现（真实系统里对应的是带 split sizes 的变体），所以不均分的正确性由「numpy 参照的语义推广 + 往返一致性（4.4）」共同背书。

#### 4.3.4 代码实践

**实践目标**：亲手确认「参照是活的」——破坏参照，测试应当失败。

**操作步骤**：

1. 复制 `test/test_plan.py` 为 `/tmp/test_plan_mutated.py`（不要动源码树）。
2. 把 `reference_scatter_head` 里的 `axis=1` 改成 `axis=2`（模拟一个写错的参照）。
3. 运行 `pytest /tmp/test_plan_mutated.py -x -q`。

**需要观察的现象**：大量用例失败，错误信息形如 `rank r mode 0` 的 `assert_array_equal` 不匹配（形状断言也可能先失败，取决于切分）。

**预期结果**：全红。这证明测试的分辨力来自参照与重放的**独立**实现路径；若改完仍然全绿，说明测试在比较两个同源结果——那是测试设计的重大缺陷。

**待本地验证**：具体失败在哪一条断言（形状还是数值）取决于参数化分支，值得看一眼但不影响结论。

#### 4.3.5 小练习与答案

**练习 1**：`reference_scatter_head` 里 `x[:, :, head_off[p]:head_off[p+1], :]` 取的是「输入 x 的头列」。为什么 mode 0 的参照按**头**切输入、按**序列**拼接输出，而不是反过来？

**答案**：mode 0 是 scatter head：输入持有的头轴是全组头（n_total），序列轴是本 rank 分片（s_r）。rank p 想要的正是「每个 r 的序列段 × p 的头」，所以对每个输入取头切片（`H_p:H_{p+1}`），沿序列轴拼接后序列变全、头变成 p 自己的分片。反过来切会得到「全头 × 部分序列」，那是 mode 1 的方向。

**练习 2**：锚定测试为什么用 `torch.manual_seed(0)` 和 `randn`？用全 1 张量行不行？

**答案**：不行。全 1 张量在任何排列下都相等，重放与参照即使各自错了也会「一致」。`make_inputs` 的 docstring（L102）说得直白：每个位置的值都互不相同，任何错放的字节才会显形。锚定测试用随机张量同理——并且它与重放走的是**同一份** `inputs`（L273 narrow 出来再 L293 转 numpy），所以比较是公平的。

**练习 3**：锚定测试在单进程里「模拟」all_to_all_single（L279-L281 的 chunks 收集）。为什么不让它真开一个 torch.distributed 进程组？

**答案**：那会破坏本测试的核心卖点——无 GPU、无进程组、无通信后端依赖（docstring L1-L2 与 develop.md L50-L52 都强调这一点）。分块语义「r 的第 j 块给 j 的第 r 槽」是文档化的确定性行为，单进程复现它既保住了运行前提，又把参照绑到了 torch 生态的公共语义上。

### 4.4 往返一致性、窗口尺寸断言与两个特殊分支

#### 4.4.1 概念说明

单个模式的对照之外，`check()` 还封装了两条全局性质，`test_round_trip` 则验证两模式合用时的恒等式：

1. **窗口尺寸全 rank 一致且等于最大输出**：真实系统里每个 rank 都要以相同偏移写 peer 的窗口（u3-l2），所以 `window_numel` 必须全组相同，且**不小于最大 rank 的输出**。注意在不均分下，最大输出往往不属于「我」——这条断言防的是「每个 rank 只按自己输出算窗口」的实现错误。
2. **往返一致性**：\[ \text{gather}(\text{scatter}(x)) = x \quad \text{（逐字节）} \] 这正是两次 all-to-all 包夹注意力（u1-l2）时数据经历的变换，也是 u2-l4 里 DiT 示例、u9-l2 里 GPU correctness worker 反复使用的性质。布局层面它是 mode 1 = mode 0 精确逆的表述。

另外两个「特殊输入」各自命中 `a2a_plan.cc` 里不同的守卫分支，是本讲要求掌握的第三、四点：

- **零长度 shard**（`seq_splits` 某项为 0，合法；负数非法——validate L132-L140 允许 0 拒绝负数）。
- **非 2 幂 world_size**：plan 层无感，传输层的 XOR 枚举需要补漏 sweep。

#### 4.4.2 核心流程

`check()`（[test/test_plan.py:L121-L148](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L121-L148)）的完整流程：

```text
inputs = make_inputs(...)                       # 互异值
plans  = [build_plan(..., r, ...) for r in range(ws)]
raw    = run_plan(inputs, plans)                # 重放
expected = reference_scatter_head / gather_head # 参照
逐 rank: 形状断言 + 逐位相等断言
窗口断言:
    { p["window_numel"] for p in plans } 只有 1 个元素   # 全 rank 相同
    且 == max(prod(p["output_shape"]))                   # 等于最大输出元素数
```

零长度 shard 命中的守卫（回顾 u3-l2 精读过的发射循环）：

```text
mode 0（scatter head）: if (row == 0 || s_me == 0) continue;
    row = head_splits[p]·d_bytes —— peer p 没有头 → 没人给它发
    s_me —— 我没有序列 → 我谁也不发（包括自己）
mode 1（gather head）:  if (row == 0 || s_p == 0) continue;
    row = n_me·d_bytes —— 我没有头 → 我谁也不发
    s_p —— peer p 没有序列 → 我跳过它
```

非 2 幂 world_size 的传输层分支（plan 层只是普通循环）：

```text
XOR 枚举: for k in 1..ws-1: peer = rank ^ k; if peer < ws: 发射
sweep:    若 ws 非 2 幂: for p in 0..ws-1: p != rank 且 (p ^ rank) >= ws → 补发射
自份额:   emit(rank, stream)  # 走调用者流，不经 xfer 流
```

#### 4.4.3 源码精读

- [test/test_plan.py:L145-L147](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L145-L147) —— 窗口尺寸双重断言：全 rank 的 `window_numel` 集合大小为 1，且弹出的值等于各 rank 输出元素数的最大值。L141-L144 的注释解释了为何 `run_plan` 的越界检查（窗口=本 rank 输出大小）已经隐式查过一次上界。
- [test/test_plan.py:L236-L257](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L236-L257) —— `test_round_trip`：同一对 splits 先 mode 0 重放、把结果按各自 output_shape 复原成张量，再 mode 1 重放，断言逐 rank 与原始输入 `assert_array_equal`。docstring 点明「这就是两模式包夹注意力的用法」。
- [test/test_plan.py:L178-L181](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L178-L181) —— `test_empty_shard`：`seq_splits=[3,0,5,4]`，docstring：「一个 rank 完全没有序列是合法的，且不得为它发射拷贝」。
- [src/a2a_plan.cc:L180-L183](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L180-L183) —— mode 0 的跳过守卫 `if (row == 0 || s_me == 0) continue;`：空序列的 rank 不发、无头的 peer 不收。
- [src/a2a_plan.cc:L199-L203](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L199-L203) —— mode 1 的镜像守卫 `if (row == 0 || s_p == 0) continue;`。
- [src/transfer.cu:L102-L107](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/transfer.cu#L102-L107) —— XOR 枚举：`peer = rank ^ k`，只发 `peer < ws` 的。注释（L84-L85）说明 XOR 顺序让 rank 两两配对而无需协调。
- [src/transfer.cu:L108-L115](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/transfer.cu#L108-L115) —— **非 2 幂 sweep 分支**：`ws & (ws-1) != 0` 时，补发所有 `p != rank && (p ^ rank) >= ws` 的 peer。L116 的 `emit(rank, stream)` 把自份额放到调用者流。
- [test/test_plan.py:L317-L333](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L317-L333) —— 文件末尾的溢出用例，L317-L323 的块注释是一个**文档化的负控制**：删掉 `a2a_plan.cc` 里的 `checked_mul`/`checked_sum` 再构建，这两个用例就会从「抛异常」变成「返回负的 window_numel」。这是「测试必须还能失败」思想在布局层的体现（u9-l3 展开）。

再划一次重点：**ws=5 不会在 test_plan.py 里执行 sweep 分支**——那段是 CUDA 代码，host-only 测试碰不到它。test_plan 为 ws=5 做的，是给「XOR 枚举会漏 peer 的那种几何」验证 plan 寻址；sweep 分支本身在 GPU 上由分布式套件覆盖——[docs/develop.md:L54-L57](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/develop.md#L54-L57) 写明：≥3 GPU 时 worker 会额外跑 ws=3，「奇数 world_size 练到非 2 幂的 peer sweep」。

#### 4.4.4 代码实践

**实践目标**：跑通现有的两个「特殊分支」测试，并对照源码守卫确认每条跳过确实有代码路径支撑。

**操作步骤**：

```bash
pytest test/test_plan.py::test_empty_shard test/test_plan.py::test_round_trip -v
```

（`test_empty_shard` 参数化为两个 mode，共 3 个用例；`test_round_trip` 1 个。）

然后对照读一遍：mode 0 下 rank 1（`s_me=0`）的 plan 应有 0 条 op；mode 1 下每个 rank 的 plan 都应比 4 条少 1 条（跳过 peer 1）。

**需要观察的现象**：全部通过；若在 4.1.4 的脚本里把 rank 换成 1、seq_splits 换成 `[3,0,5,4]`、mode 0，`ops` 应为空张量 `(0, 10)`，而 `window_numel` 仍是非零（不均分下最大输出来自别的 rank）。

**预期结果**：mode 0 rank 1 → 0 ops；mode 1 任意 rank → 3 ops（peers 0/2/3，跳过 peer 1 与……注意 mode 1 不会跳过自己，peer 集是 {0,2,3, 自己=2 时}。对 rank 2：peers {0,2,3} 中含自己，共 3 条；对 rank 0：peers {0,2,3} 含自己，共 3 条）。`window_numel` 全 rank 相同。

**待本地验证**：mode 1 各 rank 的确切 op 数（上面按守卫推演，动手打印确认）。

#### 4.4.5 小练习与答案

**练习 1**：`check()` 已经断言了「每个 rank 的窗口装得下自己的输出」（隐式，通过重放不越界）。那 L145-L147 的显式窗口断言多查了什么？

**答案**：多查了两件重放查不到的事。其一，**全 rank 一致**：重放里每个 rank 的窗口是独立分配的，尺寸不同也不会撞；真实系统里 peer 按自己的偏移写你的窗口，尺寸不一致就会写错位甚至越界（对称内存 rendezvous 也会挂起）。其二，**等于最大而非等于自己**：一个只按本 rank 输出算窗口的实现，在「我不是最大 rank」时会把窗口算小，而小 rank 自己的重放恰恰不会越界（它收的就是小的），只有跨 rank 的显式比较能抓出来。注释 L142-L144 明说了不均分时最大输出「往往不是你自己的」。

**练习 2**：`seq_splits=[3,0,5,4]`、`head_splits=[2,2,2,2]`、b=1、d=2、float32。mode 1 下 rank 0 会给哪些 peer 发射？为什么 `window_numel` 不为 0 而它自己的输出是 0 的是……（哪个 rank 的输出是 0？）

**答案**：mode 1 下 rank 0 的 peer 集合为 {0, 2, 3}（跳过 s_p=0 的 peer 1；无头守卫不触发，n_me=2）。`window_numel = 1 × max_r(s_r) × 8 × 2 = 5×8×2 = 80`，来自 s=5 的 rank 2。输出为 0 的是 rank 1 自己：`output_shape = [1, 0, 8, 2]`——它仍然持有窗口、参与屏障（u5-l3），只是既不收也不发有效数据。

**练习 3**：为什么说「ws=5 写进 test_plan.py」与「sweep 分支被覆盖」是两件事？各自在哪一层？

**答案**：sweep 是 `transfer.cu`（CUDA 传输层）为补救 XOR 枚举漏 peer 而写的分支，只有 GPU 上真实 `launch_a2a_ce` 才执行它——由分布式套件的 ws=3 运行覆盖（develop.md L54-L57）。test_plan.py 是纯主机重放，不执行任何 CUDA；ws=5 在这里的价值是把「XOR 会漏、需要 sweep 的那种奇数几何」下的**寻址**也纳入 host-only 验证（前缀和、窗口最大值、守卫跳过在奇数 ws 下的行为）。分层记忆：plan 不管 2 幂，传输管。

## 5. 综合实践

**任务**：为 `test/test_plan.py` 增加一个 ws=5（非 2 的幂）且 `seq_splits` 含一个 0 的新用例——先在纸上预测 `window_numel` 与每个 rank 的 op 数，再写断言验证，最后对照 `launch_a2a_ce` 的 sweep 分支说明 ws=5 额外覆盖了什么。

### 第一步：纸上预测

设定：b=1、d=4、elem_size=4（float32）、`seq_splits=[4,0,3,5,3]`、`head_splits=[2,2,1,2,1]`。

基本量：\( s_{\text{total}} = 15 \)，\( n_{\text{total}} = 8 \)。

**mode 0（scatter head）**：

- 每 rank 输出元素数 = \( 1 \times 15 \times n_p \times 4 \)：rank 0/1/3 → 120，rank 2/4 → 60。
- `window_numel = max = 120`，且**五个 rank 全部返回 120**。
- op 数：发射条件是 `row>0 且 s_me>0`。所有 `head_splits` 都非 0，所以只有 `s_me=0` 的 rank 1 不发射——**rank 1 → 0 条；rank 0/2/3/4 → 各 5 条**（b=1，depth=1，无批折叠；每个 peer 一条）。若把 b 改为 2，op 数不变（rows>1 时 push_batched 折叠成 depth=2 的单条），可作为附加断言。

**mode 1（gather head）**：

- 每 rank 输出元素数 = \( 1 \times s_r \times 8 \times 4 \)：rank 3 → 160，rank 0/2/4 → 96/96/96，rank 1 → 0。
- `window_numel = 160`，五个 rank 全部相同。
- op 数：发射条件是 `row>0 且 s_p>0`。所有 `n_me` 非 0，但 peer 1 的 `s_p=0`——**每个 rank（含 rank 1？不含，rank 1 的 n_me=2>0 但它要发给的 peers 中跳过 1，其余 4 个都发）**：仔细按守卫推——rank 1 的 `n_me=2` 非 0、各 peer 的 `s_p` 中只有 p=1 为 0，所以 rank 1 也发 4 条（发给 0/2/3/4，含吗？含 p∈{0,2,3,4}，不含自己因为自己就是 p=1 被跳过）。**结论：所有 5 个 rank 各发 4 条**（peer 集 = {0,2,3,4}，恰好没人给自己发，因为「自己」就是被跳过的 rank 1；对其他 rank，peer 集含自己，如 rank 0 发给 {0,2,3,4}）。

### 第二步：写断言（示例代码）

把下面加到你的 `/tmp/test_plan_mutated.py`（或本地分支的 test_plan.py——不要直接改源码树，除非你打算提 PR）：

```python
# 示例代码：ws=5（非 2 幂）且含零长度 shard 的新用例
@pytest.mark.parametrize("mode", [SCATTER_HEAD, GATHER_HEAD])
def test_uneven_nonpow2_with_empty_shard(mode):
    seq_splits, head_splits = [4, 0, 3, 5, 3], [2, 2, 1, 2, 1]
    plans = check(1, 4, seq_splits, head_splits, mode)  # 布局 + 窗口断言全包
    if mode == SCATTER_HEAD:
        expected_ops = [5, 0, 5, 5, 5]
        assert {p["window_numel"] for p in plans} == {120}
    else:
        expected_ops = [4, 4, 4, 4, 4]
        assert {p["window_numel"] for p in plans} == {160}
    got_ops = [len(p["ops"]) for p in plans]
    assert got_ops == expected_ops, got_ops
```

**需要观察的现象**：两 mode 全绿；故意把 `expected_ops` 里 rank 1 的 0 改成 5，应看到 mode 0 失败——确认断言真的在检查。

**预期结果**：按第一步的推演通过。若 op 数不符，回到 [src/a2a_plan.cc:L180-L203](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L180-L203) 的两个守卫逐 rank 核对。

**待本地验证**：op 数断言依赖「b=1 时每个 peer 恰好一条 op」——由 push_batched 的 `depth=1` 路径保证（[src/a2a_plan.cc:L83-L88](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L83-L88)），动手跑一次确认。

### 第三步：对照 sweep 分支

对 ws=5（二进制 `101`），逐一算 `rank ^ k`（k=1..4）能到谁、漏了谁：

| rank | XOR 覆盖的 peer | 漏掉的 peer（`p^rank >= 5`） |
|---|---|---|
| 0 | 1, 2, 3, 4 | 无 |
| 1 | 0, 3, 2 | **4**（1^4=5） |
| 2 | 3, 0, 1 | **4**（2^4=6） |
| 3 | 2, 1, 0 | **4**（3^4=7） |
| 4 | 0 | **1, 2, 3**（4^1=5, 4^2=6, 4^3=7） |

结论（也是本实践的书面答案）：ws=5 时，除 rank 0 外每个 rank 都有只能靠 [src/transfer.cu:L108-L115](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/transfer.cu#L108-L115) 的 sweep 分支送达的 peer——rank 4 甚至只靠 XOR 碰到 rank 0。把 ws=5 纳入 host-only 套件，等于让「sweep 是必需品」的那种几何的寻址也被重放验证；而 sweep 分支自身的执行在 GPU 上由分布式套件的奇数 ws（3）运行覆盖。两层合起来，非 2 幂路径才同时有「寻址对」和「确实送到了」两份证据。

## 6. 本讲小结

- `src/plan_bindings.cc` 是 torch 与纯主机 plan 唯一相遇的翻译单元：`a2a_plan_debug` 调用与 GPU 路径**同一个** `build_plan`，把 CopyOp 摊平成 N×10 的 int64 表——测试拿到的就是设备要执行的拷贝清单。
- `test_plan.py` 用 numpy 字节缓冲**重放**这份清单（每个模拟 rank 一个窗口、`windows[op.peer]` 决定目的地），与独立实现的参照逐位比较；重放通过即证明寻址正确，GPU 上的后续失败可以归因到传输或同步。
- 参照分两层：`reference_scatter_head`/`reference_gather_head` 是两模式的 numpy 权威表述（按头切、沿序列拼 / 按序列切、沿头拼），`test_matches_torch_all_to_all_single` 再把参照锚定到 `all_to_all_single` + permute 的生态公共语义上，防止自说自话。
- `check()` 的窗口断言查两件重放查不到的事：`window_numel` 全 rank 一致、且等于**最大** rank 的输出（不是自己的）。
- 零长度 shard 命中 `build_plan` 的两个跳过守卫（mode 0 的 `s_me==0` 不发、mode 1 的 `s_p==0` 跳过该 peer）；非 2 幂只对传输层的 XOR 枚举有意义，由 `transfer.cu` 的 sweep 分支补漏——plan 层对 2 幂无感。
- 文件末尾的溢出用例带文档化负控制：删掉 `checked_mul`/`checked_sum` 它们必须从「抛异常」变「返回负窗口」，证明测试有分辨力。

## 7. 下一步学习建议

布局层到此完整闭环。接下来两条路：

1. **进入设备层（u5-l1）**：`launch_a2a_ce` 如何把本讲重放的 CopyOp 交给 `cudaMemcpy2D/3DAsync`、XOR 顺序与单 xfer 流的理由、自份额为何走调用者流——你会不断看到「本讲的 ops 表」在真实硬件上逐条落地。
2. **对称内存窗口（u4-l1）**：重放里每个 rank 的窗口是 numpy 缓冲；真实系统里它是 `empty_strided_p2p` 分配的对称内存窗口，`window_numel` 的「全组同尺寸」断言在那里变成硬性的 peer 偏移契约。

若想先巩固测试方法论，可以跳读 u9-l1（分层测试架构）与 u9-l3（对抗性测试），本讲的负控制注释是那两讲主题的预演。另外建议动手跑一次 `pytest -m "not multigpu"`（develop.md L46），亲眼看这份无 GPU 套件在你的机器上全绿。
