# 讲义标题：T.Persistent 与数据块调度

## 1. 本讲目标

本讲讲解 TileLang-Ascend 中 `T.Persistent` 这个调度原语。学完后你应该能够：

1. 说清楚「为什么把一块大矩阵切成小 tile 后，tile 在多核间的**调度顺序**会影响 L2 cache 命中率」。
2. 会用 `T.Persistent(domain, wave_size, index, group_size=...)` 让每个 AI Core 在一个常驻循环里轮流处理多个相邻 tile，而不是「一个 block 只算一个 tile 就退出」。
3. 读懂 `src/ir.cc` 里 `PersistentFor` 的索引算法：它是怎么用一个线性下标 `rem = wave * wave_size + index` 把 tile 坐标 `(bx, by)` 反解出来，并用 `group_size` 把相邻若干核「绑」到同一个数据行上的。
4. 对比普通 `T.Kernel`（每核一 tile）与 `T.Persistent`（每核多 tile、缓存友好）两种写法在结构上的差异。

本讲依赖你已经学过 [u3-l3 矩阵计算 gemm_v0 / mma](u3-l3-gemm-mma.md)，知道 `T.gemm_v0`、`T.copy`、`T.alloc_L1/L0C` 怎么用，也依赖 [u2-l2](u2-l2-kernel-launch.md) 里关于 `T.Kernel` 与 `cid` 的概念。

---

## 2. 前置知识

在进入源码前，先用三个直觉把背景补齐。

### 2.1 Ascend 的「核」与「block」

华为昇腾 NPU 里有若干个 AI Core（例如 A2 有 20 个、A3 有 24 个），可以类比为 GPU 里的若干个 SM。在 TileLang 里，`with T.Kernel(block_num, is_npu=True) as (cid, _)` 启动 `block_num` 个逻辑 block，`cid` 就是当前 block 的编号（对应底层 `blockIdx.x`）。最朴素的用法（见 [u1-l4](u1-l4-first-gemm.md)）是 **一个 block 算一个 tile**：用 `bx = cid // n_num; by = cid % n_num` 把 `cid` 还原成 tile 的二维坐标，算完就退出。

### 2.2 L2 cache 与「数据复用」

Ascend 的 Cube 核做 GEMM 时，矩阵 A、B 的数据先从 GM（全局内存）搬到片上 L1。在搬运路径上，多核会**共享 L2 cache**。如果两个核**几乎同时**需要读同一块 A 的数据，那么先读到的核把数据放进 L2，第二个核就能直接命中，省掉一次 GM→L2 的搬运。反过来，如果各核读的数据东一块西一块，L2 里的数据就会被反复换进换出（thrash），浪费带宽。

所以问题不是「算得快不快」，而是「**谁和谁挨在一起算，能不能共享同一份缓存数据**」。

### 2.3 持久化（persistent）的含义

朴素模式下「一个 block 算一个 tile」，block 数 = tile 数，硬件自己决定哪些 block 落到哪些核、按什么顺序跑——这个顺序对缓存未必友好。

**持久化**的思路反过来：只启动「核数」个 block，每个核拿到一个 `cid` 后**不退出**，而是在一个 `for` 循环里**反复领取新 tile**，直到把整片矩阵算完。这样我们就能**在源码层面精确控制**「第几个 tile 由哪个核、在哪个时刻算」，从而把相邻、能共享数据的 tile 编排给相邻的核，让 L2 命中率最大化。这就是 `T.Persistent` 要做的事——官方文档原话是：它「让数据在多个 AI Core 间负载更均衡，并且提高数据缓存的命中概率」（见 [docs/TileLang-Ascend Programming Guide.md:1181](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md#L1181)）。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tilelang/language/persistent.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/persistent.py) | 前端 `T.Persistent` 的 Python 包装，极薄，只负责把参数透传给 C++ FFI。 |
| [src/ir.cc](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/ir.cc) | `PersistentFor` 函数（L113–L180）在这里，**全部调度逻辑都在这一段**：把线性下标反解成 tile 坐标、用 `group_size` 分组、插入尾波 `loop_break`。 |
| [tilelang/language/__init__.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/__init__.py) | 把 `Persistent` 导出为 `T.Persistent`（L32）。 |
| [examples/gemm/example_gemm_persistent.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm_persistent.py) | Developer 模式的 Persistent GEMM 示例，结构最简洁，是本讲的主线例子。 |
| [examples/gemm/example_gemm_intrinsic_persistent.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm_intrinsic_persistent.py) | Expert 模式（手写 flag 流水 + `T.mma`）的 Persistent GEMM，性能更高，展示了 `T.Kernel(core_num)` 的规范写法。 |
| [examples/gemm/example_gemm.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm.py) | 朴素「一 block 一 tile」的 GEMM，用来和 Persistent 版做对比。 |

> 说明：和 `T.Pipelined` 不一样，`T.Persistent` **没有专门的 transform pass**——搜索 `src/transform` 找不到任何 `persistent` 关键字。它的全部魔法都集中在 `src/ir.cc::PersistentFor` 里，在构建 TIR 的那一刻就把坐标计算写死了，后续 pass 看到的就是一个普通的 `kSerial` 循环加几个 `LetStmt`。

---

## 4. 核心概念与源码讲解

### 4.1 为什么需要「持久化调度」：缓存友好的数据块映射

#### 4.1.1 概念说明

把一个大 GEMM \(C = A \times B\) 切成 tile：A 切成 `m_num` 个行块，B/C 切成 `n_num` 个列块，一共有 `m_num × n_num` 个输出 tile。每个 tile \(C_{bx,by}\) 需要 A 的第 `bx` 个行块和 B 的第 `by` 个列块。

关键观察：**同一行（同一个 `bx`）的所有 tile 共用同一块 A 的行数据**；**同一列（同一个 `by`）的所有 tile 共用同一块 B 的列数据**。如果我们能让「读同一块 A 的若干个 tile」被「相邻的几个核、在相近的时刻」处理，这块 A 在 L2 里就能被反复命中，而不必每个核都重新从 GM 拉一遍。

朴素调度（`bx = cid//n_num, by = cid%n_num`，按行优先铺开）虽然也是「同行 tile 相邻」，但它把决定权交给了硬件 block 调度器，且每个 block 只算一个 tile、算完即退，缺乏「让一个核连续吃下多个相关 tile」的能力。`T.Persistent` 给我们的是一个**可编程的、显式的 tile→核 映射**，并且引入了 `group_size` 这个旋钮来精细控制「几个相邻核共享同一份数据」。

#### 4.1.2 核心流程

持久化调度的整体流程：

1. 启动 `core_num` 个 block（每个物理核一个），每个核拿到自己的 `cid`。
2. 把全部 `domain_size = m_num × n_num` 个 tile **线性排成一行**，下标记作 `rem`，取值 `0 .. domain_size-1`。
3. 把这一行切成若干 **wave（波）**，每波 `wave_size = core_num` 个 tile：第 `w` 波由 `rem = w*core_num + cid` 决定。即核 `cid` 在第 `w` 波处理第 `w*core_num + cid` 个 tile。
4. 一个核在一个 `for w in range(waves)` 的循环里，每轮算出自己这一波的 `rem`，再把 `rem` 反解成 `(bx, by)`，做一次完整的 tile 计算。
5. 最后一波如果 `domain_size` 不能被 `core_num` 整除，多出来的核（`rem >= domain_size`）用 `loop_break` 跳过，不干活。
6. `group_size` 改变 `rem → (bx,by)` 的反解方式，使得相邻 `group_size` 个 `rem` 共享同一个 `bx`，从而相邻 `group_size` 个核共享同一块 A 行数据。

「波」的个数是：

\[
\text{waves} = \left\lceil \frac{\text{domain\_size}}{\text{wave\_size}} \right\rceil
\]

#### 4.1.3 源码精读

`PersistentFor` 一开头就算出 `domain_size` 和 `waves`：

[src/ir.cc:120-125](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/ir.cc#L120-L125) —— 把 `domain`（例如 `[m_num, n_num]`）各维相乘得到 `domain_size`，再除以 `wave_size`（即 `core_num`）向上取整得到波数 `waves`。

```cpp
PrimExpr domain_size = domain[0];
for (int i = 1; i < domain.size(); i++) domain_size *= domain[i];
auto waves = ceildiv(domain_size, wave_size);
```

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：建立「tile 总数 / 核数 / 波数」的直觉。
2. **步骤**：取 `M=N=1024, block_M=128, block_N=256`，算出 `m_num=8, n_num=4, domain_size=32`；若 `core_num=24`，则 `waves=ceil(32/24)=2`。
3. **观察**：核 `cid=0..23` 在第 0 波处理 `rem=0..23`，在第 1 波处理 `rem=24..47`，其中 `rem>=32` 的会被尾波保护跳过。
4. **预期结果**：你能口算出每个核在两波里各处理哪些 `rem`，并理解为什么 `waves` 至少为 1。

#### 4.1.5 小练习与答案

**练习**：若 `domain_size=64`、`core_num=24`，每个核平均处理几个 tile？最后一波有几个核空转？
**答案**：`waves=ceil(64/24)=3`。前两波共覆盖 `48` 个 tile，第 3 波只有 `64-48=16` 个 tile 有活，故第 3 波有 `24-16=8` 个核空转（靠 `loop_break` 跳过）。每个核平均 `64/24≈2.67` 个 tile。

---

### 4.2 T.Persistent 的接口与四个参数

#### 4.2.1 概念说明

前端 `T.Persistent` 是一个极薄的包装，它把四个参数透传给 C++ 的 `_ffi_api.Persistent`：

[.tilelang/language/persistent.py:10-29](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/persistent.py#L10-L29) —— 整个前端就这一段：

```python
def Persistent(domain, wave_size, index, group_size=8):
    return _ffi_api.Persistent(domain, wave_size, index, group_size)
```

四个参数的含义：

| 参数 | 含义 | 典型取值 |
| --- | --- | --- |
| `domain` | tile 网格各维大小组成的列表，**第 0 维对应循环的第一个变量** | `[T.ceildiv(M, block_M), T.ceildiv(N, block_N)]`，对应 `(bx, by)` |
| `wave_size` | 一个波里有几个 tile，通常 = 物理核数 | `core_num` |
| `index` | 当前核在波内的编号，通常 = `cid` | `cid` |
| `group_size` | 把「最后一维」按多大粒度分组，控制相邻几个核共享数据；默认 8 | `8`（会被自动 clamp 到不超过最后一维） |

注意循环变量的绑定顺序：`for bx, by in T.Persistent([m_num, n_num], ...)` 里，`bx` 绑定 `domain[0]`（M 方向），`by` 绑定 `domain[1]`（N 方向）。这点和普通嵌套循环一致。

#### 4.2.2 核心流程

`T.Persistent(...)` 返回的不是一个普通 `for`，而是一个 `ForFrame`。当 `with`/`for` 块结束时，IR builder 会调用 `f_make_for_loop`（见 4.3 节）把它「物化」成一个真正的 TIR `For` 节点 + 若干 `LetStmt`。也就是说，`bx`、`by` 这两个变量在最终 TIR 里**不是循环变量**，而是被 `let` 绑定到由 `w`（真正的循环变量）和 `cid` 计算出来的表达式上。

#### 4.2.3 源码精读

[tilelang/language/__init__.py:32](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/__init__.py#L32) 把 `Persistent` 暴露进 `T` 命名空间：

```python
from .persistent import Persistent  # noqa: F401
```

而 [src/ir.cc:338](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/ir.cc#L338) 把 C++ 的 `PersistentFor` 注册为 FFI `tl.Persistent`，于是 Python 的 `_ffi_api.Persistent` 调用的就是它：

```cpp
TVM_REGISTER_GLOBAL("tl.Persistent").set_body_typed(PersistentFor);
```

#### 4.2.4 代码实践

1. **目标**：确认 `domain` 维度顺序与循环变量绑定。
2. **步骤**：打开 `examples/gemm/example_gemm_persistent.py`，看 [L30-L31](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm_persistent.py#L30-L31)：`for bx, by in T.Persistent([T.ceildiv(M, block_M), T.ceildiv(N, block_N)], core_num, cid)`。
3. **观察**：`domain[0]=M/block_M` 绑给 `bx`，`domain[1]=N/block_N` 绑给 `by`；`wave_size=core_num=24`，`index=cid`，`group_size` 用默认 8。
4. **预期结果**：你能在源码里指认四个实参分别对应哪个形参。

#### 4.2.5 小练习与答案

**练习**：如果把 `domain` 写成 `[T.ceildiv(N, block_N), T.ceildiv(M, block_M)]`（把 N 放前面），`for bx, by` 的含义会变吗？
**答案**：会。此时 `bx` 绑定 N 方向、`by` 绑定 M 方向，与名字暗示的相反，后续用 `bx*block_M` 去索引 A 行就会出错。所以 **`domain` 的维度顺序必须和循环变量一一对应**，这是最容易写错的地方。

---

### 4.3 持久化索引算法：ir.cc 中的 PersistentFor 精读

这是本讲最核心的一段，所有「缓存友好」的魔法都在这里。

#### 4.3.1 概念说明

我们要解决的是一个「反进位」问题：给定一个线性下标 `rem ∈ [0, domain_size)`，怎么把它映射成二维 tile 坐标 `(bx, by)`，使得**相邻的 `group_size` 个 `rem` 拥有相同的 `bx`**？

普通行优先映射 `bx = rem // n_num; by = rem % n_num` 做不到这一点——它让相邻 `n_num` 个 `rem` 共享 `bx`，粒度被 `n_num` 钉死了。`PersistentFor` 的做法是**重排维度**：先把最后一维（N 方向）按 `group_size` 切成「组」，再把线性下标按 `[N/group_size, M, group_size]` 这个**新的维度顺序**去分解。这样最快变化的是「组内位置」（粒度正好是 `group_size`），相邻 `group_size` 个 `rem` 自然落在同一组、同一个 `bx` 上。

#### 4.3.2 核心流程

设 `domain = [m_num, n_num]`（二维），`group_size` 已被 clamp 为 `g = min(group_size, n_num)`。算法分三步：

**第 1 步：构造「分组后的维度」`grouped_domain`。**

\[
\text{grouped\_domain} = \left[\; \left\lfloor \tfrac{n\_num}{g} \right\rfloor,\;\; m\_num,\;\; g \;\right]
\]

即把原来的两维拆成三维：高位是「N 方向第几个组」，中间是 M 方向 `bx`，最低位（最快变化）是「组内第几个」。

**第 2 步：把线性下标 `rem` 按上面三维做 mixed-radix 分解**（从最低位往上取余）：

\[
\begin{aligned}
\text{idxs}[2] &= rem \bmod g \\
rem' &= rem \;\lfloor/ \; g \\
\text{idxs}[1] &= rem' \bmod m\_num \\
\text{idxs}[0] &= rem' \;\lfloor/ \; m\_num
\end{aligned}
\]

其中 `idxs[1]` 就是 `bx`，`idxs[0]` 是「N 组号」，`idxs[2]` 是「组内号」。

**第 3 步：拼回 `(bx, by)`。**

\[
bx = \text{idxs}[1], \qquad by = \text{idxs}[0] \times g + \text{idxs}[2]
\]

由于最快变化的是 `idxs[2]`（组内号，范围 `0..g-1`），所以 `rem` 每增 1，`by` 增 1 而 `bx` 不变——连续 `g` 个 `rem` 共享同一个 `bx`，也就是共享同一块 A 行数据。而 `wave_size` 个相邻 `rem` 恰好分给相邻 `core_num` 个核，于是**相邻 `g` 个核共享同一块 A 行**，达成 L2 缓存复用。

**尾波保护**：当 `rem = w*wave_size + index >= domain_size` 时，插入 `loop_break` 跳过本次循环体（这块没有对应 tile）。

#### 4.3.3 源码精读

**clamp group_size 并构造 grouped_domain**：

[src/ir.cc:127-143](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/ir.cc#L127-L143) —— 先把 `group_size` 限制到不超过最后一维 `n_num`，再把维度重排成 `[n_num/g, ..., m_num, g]`：

```cpp
group_size = min(group_size, domain[domain.size() - 1]);          // clamp
Array<PrimExpr> grouped_domain;
grouped_domain.push_back(truncdiv(domain[domain.size() - 1], group_size)); // N/g
for (int i = 0; i < domain.size() - 1; ++i)
    grouped_domain.push_back(domain[i]);                          // 中间维（M）
grouped_domain.push_back(group_size);                             // g（最低位）
```

> 提示：这段写法对**任意维数**的 `domain` 都成立——它总是把「最后一维」拆成 `(最后一维/g)` 和 `g` 两部分，中间维原样保留。二维时中间维就是 M。

**线性下标反解 + rem 计算**：

[src/ir.cc:149-156](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/ir.cc#L149-L156) —— `rem = w * wave_size + index`，然后从最低位（`grouped_domain` 末尾的 `g`）开始一路取余、整除，把 `rem` 拆进 `idxs[]`：

```cpp
PrimExpr rem = loop_var * wave_size + index;
for (int i = grouped_domain.size() - 1; i >= 1; --i) {
    idxs.Set(i, truncmod(rem, grouped_domain[i]));
    rem = truncdiv(rem, grouped_domain[i]);
}
idxs.Set(0, rem);   // 最高位剩余
```

**尾波 loop_break**：

[src/ir.cc:158-168](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/ir.cc#L158-L168) —— 当 `rem` 超出 tile 总数时，发射 `tl.loop_break` 跳过本次；并且**仅当 `waves >= 2` 时才插入**这个判断（单波时每个核都有活，无需判断）：

```cpp
auto out_if = IfThenElse(domain_size <= (loop_var * wave_size + index),
                         Evaluate(Call(..., tvm::tl::loop_break(), {})), Stmt());
if (analyzer.CanProveGreaterEqual(waves, 2)) {
    new_body = SeqStmt({out_if, body});
}
```

**把坐标绑给 bx/by（LetStmt）**：

[src/ir.cc:169-176](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/ir.cc#L169-L176) —— 外层是一个对 `w` 的普通 `kSerial` 循环；循环变量 `bx`、`by` 不是 For 的归纳变量，而是用 `LetStmt` 绑定到 `idxs` 上：`bx = idxs[1]`，`by = idxs[0]*group_size + idxs[2]`，正是 4.3.2 的公式。

```cpp
Stmt outer = For(loop_var, 0, waves, ForKind::kSerial, new_body, ...);
for (int i = 0; i < vars.size() - 1; ++i)
    outer = LetStmt(vars[i], idxs[i + 1], outer);                 // bx = idxs[1]
outer = LetStmt(vars[vars.size()-1],
                idxs[0] * group_size + idxs[vars.size()], outer); // by = idxs[0]*g + idxs[2]
```

#### 4.3.4 代码实践（手工推演索引）

用一个**具体数值**把算法跑一遍，建立直观感受。

1. **目标**：亲手验证「相邻 `group_size` 个核共享同一个 `bx`」。
2. **配置**：`M=N=1024, block_M=128, block_N=256` → `m_num=8, n_num=4, domain_size=32`；`core_num=24`；`group_size` 默认 8，被 clamp 成 `min(8, n_num)=4`，故 `g=4`；`grouped_domain = [n_num/g, m_num, g] = [1, 8, 4]`；`waves = ceil(32/24) = 2`。
3. **推演第 0 波（`w=0`，`rem = cid`）**，逐个核算 `(bx, by)`：
   - `cid=0` → `rem=0`：`idxs[2]=0%4=0`, `rem'=0`, `idxs[1]=0%8=0`, `idxs[0]=0` → `bx=0, by=0*4+0=0`
   - `cid=1` → `rem=1`：`idxs[2]=1` → `bx=0, by=1`
   - `cid=2` → `rem=2`：`bx=0, by=2`
   - `cid=3` → `rem=3`：`bx=0, by=3`
   - `cid=4` → `rem=4`：`idxs[2]=4%4=0, rem'=1, idxs[1]=1` → `bx=1, by=0`
   - `cid=5..7` → `bx=1, by=1..3`
4. **观察**：核 `cid=0,1,2,3` 全部 `bx=0`，它们读的是**同一块 A 的第 0 个行块**——这正是 L2 复用的来源；`group_size=4` 恰好等于 `n_num`，所以同一行的 4 个列块被 4 个相邻核一次性吃掉。
5. **预期结果**：你能画出一张 `cid → (bx,by)` 的表，并圈出「共享 bx 的连续核段」。

> 备注（待本地验证）：`examples/gemm/example_gemm_persistent.py` 的启动网格写的是 `T.Kernel(m_num*n_num)`（即 32 个 block），而 `core_num=24`。规范写法是启动 `core_num` 个 block（见 4.4 节 `example_gemm_intrinsic_persistent.py` 的 `T.Kernel(core_num)`）。由于每个 tile 都是「从 `init=(k==0)` 重新累加、最后整块覆写回 C」的完整计算，即便个别 tile 被多个 block 重复计算，写回的值也一致，故结果正确，但会浪费算力——所以**推荐按 `core_num` 启动**。

#### 4.3.5 小练习与答案

**练习 1**：在 4.3.4 的配置下，核 `cid=0` 在第 1 波（`w=1`）处理哪个 tile？
**答案**：`rem = 1*24 + 0 = 24`。`idxs[2]=24%4=0, rem'=6, idxs[1]=6%8=6, idxs[0]=0` → `bx=6, by=0`。

**练习 2**：把 `group_size` 从 4 改成 1（假设 `n_num` 仍为 4），相邻核还会共享 `bx` 吗？
**答案**：不会。`g=1` 时 `idxs[2]` 恒为 0，`rem` 每增 1，`idxs[1]`（即 `bx`）就增 1，相邻核的 `bx` 各不相同，丧失 A 行复用。可见 `group_size` 正是「缓存复用粒度」的开关。

---

### 4.4 实例对比：普通 GEMM 与 Persistent GEMM

#### 4.4.1 概念说明

把两种写法放在一起，差异就一目了然：

- **朴素**（`example_gemm.py`）：`T.Kernel(m_num*n_num)`，每个 block 用 `bx=cid//n_num, by=cid%n_num` 算**一个** tile，算完退出。block 数 = tile 数。
- **Persistent**（`example_gemm_persistent.py`）：核数个 block 常驻，外层 `for bx, by in T.Persistent(...)` 循环领取多个 tile，由 `PersistentFor` 决定「哪个核算哪些 tile」以利于缓存。

结构上最大的差别是：朴素版的「选 tile」是用户手写两行除法/取余；Persistent 版的「选 tile」交给 `PersistentFor` 的索引算法，并且**套在一个核内循环里**，让一个核连续处理多个 tile。

#### 4.4.2 核心流程

以 `example_gemm_persistent.py` 为例，一个 Persistent GEMM 核的结构是：

```
with T.Kernel(...) as (cid, _):          # 拿到核号
    分配 L1 / L0C 缓冲（常驻，跨 tile 复用！）
    with T.Scope("C"):                    # Cube 域
        for bx, by in T.Persistent([m_num, n_num], core_num, cid):
            for k in T.serial(loop_k):    # 沿 K 分块累加这一个 tile
                T.copy(A[bx,k], A_L1); T.copy(B[k,by], B_L1)
                T.gemm_v0(A_L1, B_L1, C_L0, init=(k==0))
            T.copy(C_L0, C[bx,by])        # 写回这一个 tile
```

注意一个朴素版没有的好处：**L1/L0C 缓冲在 `T.Persistent` 循环外分配，跨多个 tile 复用**，不必每个 tile 重新分配。

#### 4.4.3 源码精读

**朴素版：手写坐标、一核一 tile**：

[examples/gemm/example_gemm.py:31-33](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm.py#L31-L33) —— 启动 `m_num*n_num` 个 block，用除法/取余还原 `(bx, by)`，每个核只算一个 tile：

```python
with T.Kernel(m_num * n_num, is_npu=True) as (cid, _):
    bx = cid // n_num
    by = cid % n_num
```

**Persistent 版（Developer）：用 T.Persistent 领取多个 tile**：

[examples/gemm/example_gemm_persistent.py:14-31](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm_persistent.py#L14-L31) —— `core_num=24` 写死为核数；`T.Persistent([M/block_M, N/block_N], core_num, cid)` 把 tile 调度交给 `PersistentFor`，缓冲在循环外分配：

```python
core_num = 24
...
with T.Kernel(m_num * n_num, is_npu=True) as (cid, _):
    A_L1 = T.alloc_L1((block_M, K_L1), dtype)   # 循环外分配 → 跨 tile 复用
    B_L1 = T.alloc_L1((K_L1, block_N), dtype)
    C_L0 = T.alloc_L0C((block_M, block_N), accum_dtype)
    with T.Scope("C"):
        for bx, by in T.Persistent([T.ceildiv(M, block_M), T.ceildiv(N, block_N)],
                                   core_num, cid):
            ...
```

**Persistent 版（Expert / 规范启动网格）**：

[examples/gemm/example_gemm_intrinsic_persistent.py:13-48](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm_intrinsic_persistent.py#L13-L48) —— 这是更规范的写法：启动网格**就是 `core_num`**（`T.Kernel(core_num, ...)`），并用 Expert 模式的手写 `set_flag/wait_flag` 流水 + `T.mma` 做高性能计算。`core_num=20`，`T.Persistent` 的用法完全一致：

```python
core_num = 20
...
with T.Kernel(core_num, is_npu=True) as (cid, _):
    ...
    for bx, by in T.Persistent([T.ceildiv(M, block_M), T.ceildiv(N, block_N)], core_num, cid):
        ...
```

> 这个例子同时演示了「Persistent 调度 + 手写多级流水」如何叠加：外层 `T.Persistent` 管 tile→核 映射，内层 `set_flag/wait_flag` 管 Cube 核内 MTE2→MTE1→M→Fix 的搬运/计算重叠（流水细节见 [u4-l2 同步原语](u4-l2-sync-primitives.md)）。两者正交，互不干扰。

#### 4.4.4 代码实践（本讲主线实践）

**目标**：用 `T.Persistent` 重写一个 GEMM，对比与朴素 `T.Kernel` 版在结构上的差异。

**操作步骤**：

1. 先运行朴素版作为基线：`python examples/gemm/example_gemm.py`，确认打印 `Kernel Output Match!`。
2. 复制 `example_gemm_persistent.py` 为 `my_persistent_gemm.py`，做两处改造：
   - 把启动网格改成规范写法 `with T.Kernel(core_num, is_npu=True) as (cid, _)`（取一个不超过 `m_num*n_num` 的 `core_num`，例如 8）。
   - 在 `T.Persistent` 外、`T.Scope("C")` 内，对每次进入新 tile 加一条 `T.printf("cid", cid, "bx", bx, "by", by)`（调试打印用法见 [u7-l4 调试与性能分析](u7-l4-debug-profiling.md)），观察每个核领到的 tile 序列。
3. 运行 `python my_persistent_gemm.py`。
4. 把 `core_num` 在 `4 / 8 / 16` 之间切换，再分别运行。

**需要观察的现象**：

- 改造后仍打印 `Kernel Output Match!`，说明调度方式不影响数值正确性。
- `T.printf` 输出里，**相邻 `cid`（如同一个波内 cid=0,1,2,3）的 `bx` 相同**，只有 `by` 递增——这正是 4.3 节推导的「相邻核共享 A 行」。
- 改 `core_num` 只改变「每波几个 tile / 共几波」，`(cid → bx,by)` 的相对关系不变。

**预期结果**：你能画出两种写法并排的对照表（如下），并解释为什么 Persistent 版的 L1/L0C 缓冲可以提到循环外。

| 维度 | 朴素 `example_gemm.py` | Persistent 版 |
| --- | --- | --- |
| 启动 block 数 | `m_num*n_num`（=tile 数） | `core_num`（=核数，推荐） |
| 选 tile 方式 | 手写 `cid//n_num, cid%n_num` | `T.Persistent(...)` 自动索引 |
| 每核处理 tile 数 | 1 | `ceil(tiles/core_num)` 个 |
| L1/L0C 缓冲 | 每 block 独立 | 循环外分配，跨 tile 复用 |
| 缓存友好性 | 取决于硬件调度 | 由 `group_size` 显式控制 |

> 若无真实 NPU 环境，可改用源码阅读型实践：在 [src/ir.cc:149-176](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/ir.cc#L149-L176) 处手工代入 4.3.4 的数值，把 `cid=0..7` 的 `(bx,by)` 全部算出来并填进上表，验证相邻核共享 `bx`。运行结果标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 Persistent 版把 `A_L1/B_L1/C_L0` 的分配放在 `T.Persistent` 循环**外面**是安全的？
**答案**：因为这些缓冲在进入下一个 tile 前会被重新 `T.copy` 覆盖（A_L1、B_L1）或被 `init=(k==0)` 重新清零累加（C_L0），即每个 tile 都从头计算、最后整块写回 C。缓冲跨 tile 复用不会残留上一个 tile 的脏数据，反而省下了重复分配/释放的开销。

**练习 2**：朴素版能否也获得同样的 L2 缓存效果？
**答案**：理论上硬件 block 调度器**可能**恰好把同行 tile 排到相近时刻，但这不可控、不保证。Persistent 版把调度顺序写进源码（`rem = w*core_num+cid` + `group_size` 分组），是**确定性**的缓存友好编排，这是两者的本质区别。

---

## 5. 综合实践

把本讲内容串起来，完成一个小任务：**给一个固定规模的 GEMM 设计 Persistent 调度，并预测它的缓存行为。**

1. 取 `M=2048, N=1024, K=4096`，`block_M=128, block_N=128`，算出 `m_num, n_num, domain_size`。
2. 假设目标卡有 `core_num=32` 个核，回答：
   - `waves` 是多少？最后一波有几个核空转？
   - 默认 `group_size=8`（clamp 后是多少？）时，相邻几个核会共享同一个 `bx`？这些核覆盖了哪几个 `by`？
3. 基于 `example_gemm_intrinsic_persistent.py` 写出 `T.Kernel(core_num)` + `T.Persistent(...)` 的骨架（不需要写全 flag 流水），并把缓冲分配放在循环外。
4. 用 4.3 节的公式手算 `cid=0` 在第 0 波和第 1 波分别处理哪个 `(bx, by)`，验证你的骨架。

通过这个任务，你会真正理解：`T.Persistent` 不是「另一种 for 循环」，而是**一个可推导、可预测的 tile→核 调度器**，它的价值在于把 L2 缓存复用从「碰运气」变成「源码里写死」。

---

## 6. 本讲小结

- `T.Persistent` 解决的是「tile 在多核间的**调度顺序**」问题——让相邻、能共享数据的 tile 由相邻核在相近时刻处理，提升 L2 cache 命中。
- 前端 `T.Persistent(domain, wave_size, index, group_size=8)` 极薄，全部逻辑在 [src/ir.cc:113-180](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/ir.cc#L113-L180) 的 `PersistentFor`，且**没有专用 transform pass**。
- 核心算法：线性下标 `rem = w*wave_size + index`，按重排后的维度 `[N/g, M, g]` 反解成 `(bx, by)`；`group_size` 控制相邻几个核共享同一个 `bx`（即同一块 A 行数据）。
- 尾波用 `loop_break` 保护（仅 `waves>=2` 时插入），处理 `domain_size` 不能被核数整除的情况。
- 与朴素「一核一 tile」相比，Persistent 版的缓冲可提到循环外跨 tile 复用，且调度对缓存的友好性是**确定性**的、可由 `group_size` 调节的。
- 规范启动网格是 `T.Kernel(core_num)`（见 `example_gemm_intrinsic_persistent.py`），它还可与 Expert 模式的手写流水叠加，两者正交。

---

## 7. 下一步学习建议

- 想看「持久化调度 + 手写多级流水」如何叠加出高性能 GEMM，继续读 [u7-l2 高性能 GEMM 优化](u7-l2-hi-perf-gemm.md)，那里会把本讲的 `T.Persistent` 与 `T.use_swizzle`、`T.mma`、双缓冲、flag 流水组合起来。
- `T.Persistent` 经常和 Cube/Vector 跨核流水一起用在 FlashAttention 里，可先读 [u3-l6 T.Pipelined 软件流水](u3-l6-pipelined.md) 理解「核内流水」，再进 [u5-l2 跨核流水与 CrossCorePipeline](u5-l2-cross-core-pipeline.md)。
- 若想验证调度是否真的带来了缓存收益，可学习 [u7-l4 调试与性能分析](u7-l4-debug-profiling.md) 里的 `msprof` 性能采集方法。
