# 量子类型系统：qudit、qubit、qreg、qvector、qspan

## 1. 本讲目标

本讲是「编程模型」单元（u2）的第一讲。在 u1 里我们已经能用 `__qpu__` 写出最小的 C++/Python 量子内核，并跑通采样。但我们当时把「分配几个比特」「对第几个比特作用门」当成了理所当然的事，并没有深究：**CUDA-Q 用什么 C++ 类型来表示一个量子比特？多个比特怎么打包？子区间怎么取？**

学完本讲，你应当能够：

- 说清 `qudit`、`qubit`、`qvector`、`qarray`、`qreg`、`qspan`、`qview` 各自的角色、归属关系与推荐用法。
- 解释「量子比特不可拷贝、不可移动」这一约束在物理（量子不可克隆定理）和工程（C++ `= delete`）两个层面上的体现。
- 在内核里正确地分配、索引、切片量子比特，并能把一个「想拷贝量子比特」的错误写法改成正确的「传视图」写法。
- 看懂这些类型的源码头文件，并能从源码中预言某段代码能否通过编译。

本讲只讲「量子值的类型与生命周期」，**不讲门的具体语义**（ctrl/neg/adjoint 留给 u2-l2），**不讲测量与采样**（留给 u2-l3）。

## 2. 前置知识

### 2.1 物理直觉：为什么量子比特不能像普通变量那样复制

经典世界里，`int b = a;` 把 `a` 的值复制一份给 `b`，复制后 `a` 和 `b` 互不影响。我们天然期望量子比特也能这样。但量子力学有一个根本性约束——**量子不可克隆定理（no-cloning theorem）**：不存在一个酉算子 \(U\)，能够对任意未知态 \(|\psi\rangle\) 完成

\[
U|\psi\rangle|0\rangle = |\psi\rangle|\psi\rangle \quad \forall\,|\psi\rangle .
\]

直觉上的证明很短：如果这样的 \(U\) 存在，那么对任意 \(|\psi\rangle,|\phi\rangle\)，内积应满足

\[
\langle\psi|\phi\rangle \;=\; \langle\psi|\phi\rangle^2 ,
\]

而该等式并不普遍成立（除非态完全相同或正交）。因此「复制一个未知量子态」在物理上就没有合法的操作。

CUDA-Q 把这个物理定律直接焊进了 C++ 类型系统：**量子值类型删除了拷贝构造与移动构造**。当你写出 `qubit b = a;` 时，不是运行时报错，而是编译期直接拒绝。这是「物理定律 → 类型系统约束」的典型案例，理解了它，本讲所有看似奇怪的 `= delete` 就都顺理成章了。

### 2.2 工程直觉：拥有（owning）vs 不拥有（view）

经典 C++ 里，`std::vector` 是「拥有」元素的容器（构造时分配、析构时释放），而 `std::span` / `std::string_view` 只是「借看」一段已经存在的内存，自己不分配也不释放。

CUDA-Q 完全沿用这套思路，只是把「分配/释放内存」换成「向运行时申请/归还一个量子比特编号」：

| 类别 | 经典对应 | CUDA-Q 类型 | 是否拥有比特 | 可否拷贝 |
|------|----------|-------------|-------------|----------|
| 单个拥有者 | 单个对象 | `qudit` / `qubit` | 是 | 否 |
| 动态大小拥有者 | `std::vector` | `qvector` | 是 | 否 |
| 编译期大小拥有者 | `std::array` | `qarray` | 是 | 否 |
| 非拥有视图 | `std::span` | `qview` | 否 | **是** |
| （已废弃）动态拥有者 | `std::vector` | `qreg` | 是 | 否 |
| （已废弃）视图 | `std::span` | `qspan` | 否 | 是 |

记住这张表，本讲后面的内容都是在解释和验证它。

### 2.3 关键背景：ExecutionManager 与全局量子比特编号

在 u1-l4 我们已经知道：写 `cudaq::qubit q;` 时，构造函数会向一个单例 `ExecutionManager` 申请一个**全局唯一编号**（`id`），门操作只是把「门名 + 比特编号 + 参数」记录下来交给后端。本讲的类型系统，本质上就是围绕「谁来申请编号、谁来归还编号、谁能只是借用编号」这件事做设计。理解这一点，就能从源码层面预言每种类型的行为。

## 3. 本讲源码地图

本讲涉及的关键头文件全部位于 `runtime/cudaq/qis/`（qis = quantum instruction set，量子指令集）：

| 文件 | 作用 |
|------|------|
| [runtime/cudaq/qis/qudit.h](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qudit.h) | 定义最底层的量子值类型 `qudit<Levels>` 及其别名 `qubit`。 |
| [runtime/cudaq/qis/qubit_qis.h](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qubit_qis.h) | 定义 `__qpu__` 宏、`h/x/ry/...` 等门函数，以及把 qubit 翻译成 `QuditInfo` 的桥接代码。本讲主要看它的「类型识别」部分。 |
| [runtime/cudaq/qis/qvector.h](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qvector.h) | 动态大小的「拥有型」容器 `qvector`。 |
| [runtime/cudaq/qis/qreg.h](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qreg.h) | 已废弃的 `qreg`，理解它有助于看懂旧代码与迁移趋势。 |
| [runtime/cudaq/qis/qspan.h](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qspan.h) | 已废弃的视图 `qspan`，并定义了 `dyn` 常量。 |
| [runtime/cudaq/qis/qview.h](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qview.h) | 推荐使用的非拥有视图 `qview`。 |
| [runtime/cudaq/qis/qarray.h](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qarray.h) | 编译期大小的「拥有型」容器 `qarray`。 |

> 提示：`qvector.h` include 了 `qview.h`，`qreg.h` include 了 `qspan.h`，`qspan.h` 又 include 了 `qudit.h`。它们的依赖关系本身就反映了「视图依赖底层比特、容器组合多个比特」的设计层次。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 qudit 模板与 qubit**：最底层的量子值类型，一切容器的元素。
- **4.2 拥有型容器：qvector、qarray 与（废弃的）qreg**：怎么一次性拥有一组比特。
- **4.3 非拥有视图：qview 与（废弃的）qspan，以及不可拷贝约束**：怎么把一组比特「借」给别人，以及为什么拥有型类型不能拷贝。

### 4.1 qudit 模板与 qubit

#### 4.1.1 概念说明

`qudit` 是一个 \(d\)-能级量子系统的抽象（qu**d**it，d 代表 dimension/levels）。我们日常说的「量子比特（qubit）」是 \(d=2\) 的特例，即 `qudit<2>`。CUDA-Q 用模板参数 `Levels` 把能级数编码进类型，于是「二能级比特」和「一般 qudit」在类型层面就区分开了——后续你会看到，门操作会在编译期 `static_assert` 拒绝非二能级的 qudit。

每个 `qudit` 持有一个**不可变的逻辑编号 `idx`**，这是它在全局量子比特寄存器里的身份证。这个编号在构造时由 `ExecutionManager` 分配，在析构时归还，期间不变。也就是说，`qudit` 对象的生命周期就等于「这个比特被占用的这段时间」。

#### 4.1.2 核心流程

一个 `qudit` 对象的生命周期可以画成：

```
构造 qudit q;
   └─> getExecutionManager()->allocateQudit(levels)  ──> 拿到全局唯一 idx
       （此后 q.id() 永远返回这个 idx，不可改）
... 在内核里对 q 作用门（门函数读取 q.id() 记录指令）...
q 离开作用域（析构）
   └─> getExecutionManager()->returnQudit({levels, idx})  ──> 归还编号，可被复用
```

两件事注定了它「不可拷贝、不可移动」：

1. **拷贝**会制造两个对象持有同一个 `idx`，析构时同一编号被归还两次，且语义上等于「克隆量子态」，违反不可克隆定理。
2. **移动**若把源对象掏空，源对象析构时仍会调用 `returnQudit`，造成编号被错误归还。

所以源码干脆把两条路都堵死。

#### 4.1.3 源码精读

`qudit` 的定义极其紧凑，我们逐段看。首先是模板与唯一编号（[runtime/cudaq/qis/qudit.h:L17-L24](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qudit.h#L17-L24)）：

```cpp
/// The qudit models a general d-level quantum system.
/// This type is templated on the number of levels d.
template <std::size_t Levels>
class qudit {
  /// Every qudit has a logical index in the global qudit register,
  /// `idx` is this logical index, it must be
  /// provided at construction and is immutable.
  const std::size_t idx = 0;
```

注意 `idx` 是 `const`——一旦构造完成就再也不能改，这正是「不可变身份证」在语言层面的体现。

构造函数向 `ExecutionManager` 申请编号（[runtime/cudaq/qis/qudit.h:L31-L32](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qudit.h#L31-L32)）：

```cpp
/// Construct a qudit, will allocated a new unique index
qudit() : idx(getExecutionManager()->allocateQudit(n_levels())) {}
```

`allocateQudit` 在 `ExecutionManager` 里是纯虚接口，由具体后端实现分配策略（[runtime/cudaq/qis/execution_manager.h:L109-L111](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/execution_manager.h#L109-L111)）：

```cpp
/// Allocates a qudit and returns its identifier (index).
virtual std::size_t allocateQudit(std::size_t quditLevels = 2) = 0;
```

本讲的核心——「不可拷贝、不可移动」——就在构造函数下面几行（[runtime/cudaq/qis/qudit.h:L43-L46](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qudit.h#L43-L46)）：

```cpp
// Qudits cannot be copied
qudit(const qudit &q) = delete;
// qudits cannot be moved
qudit(qudit &&) = delete;
```

这两行 `= delete` 是把「量子不可克隆定理」编译期化的关键。任何 `qubit b = a;`、`qubit b(a);`、`std::vector<qubit> v; v.push_back(q);` 之类隐含拷贝/移动的写法，都会在这里被编译器拒掉。

析构函数把编号归还给池子，便于后续复用（[runtime/cudaq/qis/qudit.h:L69-L70](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qudit.h#L69-L70)）：

```cpp
// Destructor, return the qudit so it can be reused
~qudit() { getExecutionManager()->returnQudit({n_levels(), idx}); }
```

最后是最重要的一行别名——**`qubit` 就是 `qudit<2>`**（[runtime/cudaq/qis/qudit.h:L73-L74](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qudit.h#L73-L74)）：

```cpp
// A qubit is a qudit with 2 levels.
using qubit = qudit<2>;
```

所以本讲标题里的「qudit、qubit」其实是同一个东西，`qubit` 只是个更顺手的名字。`n_levels()` 是个 `static constexpr` 方法（[runtime/cudaq/qis/qudit.h:L51-L52](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qudit.h#L51-L52)），能在编译期拿到能级数，这让门函数可以做编译期断言。

门函数怎么用这个类型？以单比特门为例，`qubit_qis.h` 里把 qubit 转成 `QuditInfo`（「能级 + 编号」二元组）再交给执行管理器（[runtime/cudaq/qis/qubit_qis.h:L53-L55](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qubit_qis.h#L53-L55)）：

```cpp
// Convert a qubit to its unique id representation
inline QuditInfo qubitToQuditInfo(qubit &q) { return {q.n_levels(), q.id()}; }
```

而门函数模板 `oneQubitApply` 在最开头就断言「只能操作二能级比特」（[runtime/cudaq/qis/qubit_qis.h:L66-L68](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qubit_qis.h#L66-L68)）：

```cpp
static_assert(std::conjunction<std::is_same<qubit, QubitArgs>...>::value,
              "Cannot operate on a qudit with Levels != 2");
```

这正是 `Levels` 模板参数的价值：在类型层面把「普通 qubit」和「高维 qudit」分开，让通用门集合只接受前者。本讲后续只关心 `qubit`（即 `Levels=2`）。

#### 4.1.4 代码实践

**实践目标**：亲手触发一次「拷贝 qubit」的编译错误，建立对 `= delete` 的肌肉记忆。

**操作步骤**：

1. 新建一个 `copy_qubit.cpp`（**示例代码，非项目原有文件**），内容如下：

   ```cpp
   // 示例代码：用于观察编译器对「拷贝量子比特」的报错
   #include <cudaq.h>

   __qpu__ void kernel() {
       cudaq::qubit a;     // 合法：构造一个新比特
       cudaq::qubit b = a; // 非法：试图拷贝比特
       x(a);
       mz(a);
   }

   int main() {
       cudaq::sample(kernel).dump();
       return 0;
   }
   ```

2. 用 `nvq++` 编译（假设你已按 u1-l3 装好工具链）：

   ```bash
   nvq++ copy_qubit.cpp -o copy_qubit.x
   ```

**需要观察的现象**：编译失败。在报错信息里定位到指向 `qudit` 删除的拷贝构造的那一行，典型文案会涉及 `deleted function`、`qudit::qudit(const qudit&)`。

**预期结果**：编译器拒绝该文件，根因就是 [qudit.h:L44](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qudit.h#L44) 的 `qudit(const qudit &q) = delete;`。

**修复**：删掉 `cudaq::qubit b = a;` 这一行即可编译通过。

> 待本地验证：不同版本的 clang 给出的报错措辞略有差异，但「deleted constructor」这一根因是稳定的。

#### 4.1.5 小练习与答案

**练习 1**：`cudaq::qubit` 和 `cudaq::qudit<2>` 是同一个类型吗？为什么？

**答案**：是同一个类型。源码里 `using qubit = qudit<2>;`（[qudit.h:L74](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qudit.h#L74)），`qubit` 只是别名，二者完全等价，可互换使用。

**练习 2**：为什么 `qudit` 把 `idx` 声明成 `const`？如果把 `const` 去掉会带来什么隐患？

**答案**：`idx` 是该比特在全局寄存器里的唯一身份证，构造时由 `ExecutionManager` 分配，生命周期内必须保持不变。声明成 `const` 是为了在语言层面禁止后续修改。若去掉 `const`，理论上可以让一个比特对象「冒充」另一个编号，导致门作用到错误的比特、析构时归还错误的编号，彻底破坏运行时的比特账本。

---

### 4.2 拥有型容器：qvector、qarray 与（废弃的）qreg

#### 4.2.1 概念说明

单个 `qubit` 解决了「一个比特」的问题，但内核里经常要操作「一串比特」（比如一个寄存器）。CUDA-Q 提供**拥有型容器**：构造时一次性分配多个比特，析构时一次性归还，并提供索引、切片、迭代接口。它们都是「量子态拥有者」，因此同样**不可拷贝、不可移动**——理由和 `qudit` 完全一致（不可克隆 + 编号账本）。

目前推荐的拥有型容器有两个：

- `qvector`：动态大小（运行时决定几个比特），对应 `std::vector`。
- `qarray<N>`：编译期固定大小，对应 `std::array`。

而 `qreg` 是它们的「老前辈」，已被标注 `[[deprecated]]`，按大小是动态还是编译期分别迁移到 `qvector` 和 `qarray`。本节先讲推荐类型，再讲 `qreg` 以便你看懂旧代码。

#### 4.2.2 核心流程

拥有型容器的生命周期与 `qudit` 一致，只是「批量」：

```
构造 qvector q(n);  /  qarray<N> q;
   └─> 内部 std::vector/std::array 逐个构造 n 个 qudit
       └─> 每个 qudit 构造时向 ExecutionManager 申请一个编号
           （于是 q 拥有了 n 个连续或非连续的全局编号）
... 用 q[i] 取比特、用 q.slice(...) 取子区间（得到非拥有视图）...
q 离开作用域
   └─> 内部容器析构 ─> 每个 qudit 析构 ─> 各自 returnQudit
```

关键设计：**容器「拥有」比特，但它的 `slice/front/back` 不再创造新的比特，而是返回一个「非拥有视图」（`qview`）**。这样既能方便地切子区间，又不会违反不可克隆——视图只是借用已有编号，没有新的比特诞生。

#### 4.2.3 源码精读

先看推荐的 `qvector`。它是一个模板，默认 `Levels=2`（即默认装的是 qubit），内部用 `std::vector<qudit<Levels>>` 持有比特（[runtime/cudaq/qis/qvector.h:L17-L28](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qvector.h#L17-L28)）：

```cpp
/// @brief A `qvector` is an owning, dynamically sized container for qudits.
/// The semantics of the `qvector` follows that of a `std::vector` for qudits.
/// It is templated on the number of levels for the held qudits.
template <std::size_t Levels = 2>
class qvector {
public:
  using value_type = qudit<Levels>;
private:
  std::vector<value_type> qudits;
public:
  /// Construct a `qvector` with `size` qudits in the |0> state.
  qvector(std::size_t size) : qudits(size) {}
```

注意构造函数体 `qudits(size)`——它会默认构造 `size` 个 `qudit`，每个都各自去 `ExecutionManager` 申请编号。所以 `cudaq::qvector q(5);` 在内核里就等价于「帮我申请 5 个比特」。

`qvector` 同样删除了拷贝、移动与拷贝赋值（[runtime/cudaq/qis/qvector.h:L57-L64](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qvector.h#L57-L64)）：

```cpp
qvector(qvector const &) = delete;
qvector(qvector &&) = delete;
qvector &operator=(const qvector &) = delete;
```

索引与切片是日常最常用的接口。`operator[]` 返回某个比特的引用（[runtime/cudaq/qis/qvector.h:L72-L73](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qvector.h#L72-L73)），而 `slice/front(count)/back(count)` 一律返回**非拥有的 `qview`**（[runtime/cudaq/qis/qvector.h:L75-L94](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qvector.h#L75-L94)）：

```cpp
value_type &operator[](const std::size_t idx) { return qudits[idx]; }

qview<Levels> front(std::size_t count) {
  return std::span(qudits).subspan(0, count);
}
qview<Levels> back(std::size_t count) {
  return std::span(qudits).subspan(size() - count, count);
}
qview<Levels> slice(std::size_t start, std::size_t size) {
  return std::span(qudits).subspan(start, size);
}
```

注意它们底层就是 `std::span(...).subspan(...)`——CUDA-Q 直接复用 C++ 标准库的「不拥有视图」机制来实现量子比特的切片。这就是 4.3 节要讲的 `qview` 的来源。

`qarray<N>` 与 `qvector` 几乎对称，只是把 `std::vector` 换成 `std::array`，大小在编译期固定（[runtime/cudaq/qis/qarray.h:L31-L44](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qarray.h#L31-L44)）：

```cpp
template <std::size_t N, std::size_t Levels = 2>
  requires(detail::ValidQArraySize<N>)
class qarray : public qarray_base {
public:
  using value_type = qudit<Levels>;
private:
  std::array<value_type, N> qudits;
public:
  qarray() {}
```

两个细节值得注意：

1. `requires(detail::ValidQArraySize<N>)` 加上 `concept ValidQArraySize = N > 0`（[qarray.h:L18-L19](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qarray.h#L18-L19)）保证 `qarray<0>` 直接编译报错——零个比特的容器没意义。
2. `qarray` 继承自空基类 `qarray_base`（[qarray.h:L25](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qarray.h#L25)），这样框架可以「不带模板参数」地识别「这是不是个 qarray」。`qubit_qis.h` 里的类型特征 `IsQarrayType` 就是靠它（[runtime/cudaq/qis/qubit_qis.h:L768-L769](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qubit_qis.h#L768-L769)）：

   ```cpp
   template <typename T>
   using IsQarrayType = std::is_base_of<cudaq::qarray_base, remove_cvref<T>>;
   ```

   顺带可以看到，同一段代码还定义了 `IsQubitType`、`IsQvectorType`、`IsQviewType`（[qubit_qis.h:L759-L769](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qubit_qis.h#L759-L769)），门函数的通用 applicator 正是靠这些特征来统一处理「单比特 / 动态容器 / 视图 / 定长容器」四种实参。

最后看**已废弃**的 `qreg`。它用 `std::conditional_t` 在「动态 / 编译期」之间二选一，本质是 `qvector` 和 `qarray` 的合体（[runtime/cudaq/qis/qreg.h:L35-L49](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qreg.h#L35-L49)）：

```cpp
template <std::size_t N = dyn, std::size_t Levels = 2>
  requires(detail::ValidQregSize<N>)
class [[deprecated(
    "The qreg type is deprecated in favor of qvector (for dynamic lengths) and "
    "qarray (for constant lengths).")]] qreg {
public:
  using value_type = qudit<Levels>;
private:
  std::conditional_t<N == dyn, std::vector<value_type>,
                     std::array<value_type, N>>
      qudits;
```

`dyn` 是 `std::dynamic_extent` 的别名，定义在 `qspan.h`（[runtime/cudaq/qis/qspan.h:L26](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qspan.h#L26)）：

```cpp
inline constexpr auto dyn = std::dynamic_extent;
```

底部的推导指引把 `qreg(n)` 推成动态 qubit 寄存器（[runtime/cudaq/qis/qreg.h:L111-L112](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qreg.h#L111-L112)）：

```cpp
qreg(std::size_t) -> qreg<dyn, 2>;
```

迁移建议很明确：动态大小用 `qvector`，编译期大小用 `qarray`，两者之和就是 `qreg` 的全部能力，而且语义更清晰（不需要用同一个模板同时表达两种含义）。

#### 4.2.4 代码实践

**实践目标**：用 `qvector` 分配一组比特，分别用「整体作用门」「单比特索引」「子区间视图」三种方式操作不同的比特，并打印采样结果。这是本讲主任务的一个子步骤。

**操作步骤**：

1. 新建 `qvector_index.cpp`（**示例代码**）：

   ```cpp
   // 示例代码：演示 qvector 的整体/索引/切片三种用法
   #include <cudaq.h>

   __qpu__ void kernel() {
       cudaq::qvector q(4);   // 拥有 4 个比特

       // (a) 整体作用：对 q 里每个比特都作用 x（广播）
       x(q);

       // (b) 单比特索引：把第 0 个比特再翻转回 |0>
       x(q[0]);

       // (c) 子区间视图：对后两个比特各作用一次 h
       cudaq::qview tail = q.slice(2, 2);
       h(tail);

       mz(q);
   }

   int main() {
       auto result = cudaq::sample(kernel);
       result.dump();
       return 0;
   }
   ```

2. 编译运行：

   ```bash
   nvq++ qvector_index.cpp -o qvector_index.x
   ./qvector_index.x
   ```

**需要观察的现象**：采样输出一个 4 比特串的分布。请你根据门的作用推导：经过 (a) 全翻、(b) 第 0 位翻回、(c) 第 2、3 位各做 H 后，最终态是哪些基矢的叠加。

**预期结果**：由于 (a)+(b) 后比特 0 为 \(|0\rangle\)、比特 1 为 \(|1\rangle\)，而比特 2、3 各为 \(|1\rangle\) 经 H 后变成 \((|0\rangle-|1\rangle)/\sqrt2\)，所以采样应看到比特 1 恒为 1，比特 0 恒为 0，比特 2、3 各以约 50% 取 0/1。最终 4 比特串（按 CUDA-Q 默认的显示顺序）应集中在 4 种组合上，每种约 25%。

> 待本地验证：上述分析基于默认比特序与显示约定，实际分布请以本机采样结果为准；重点观察「比特 0、1 是确定性的，比特 2、3 是随机的」这一结构。

3. 想体验「编译期大小」，可把 `cudaq::qvector q(4);` 换成 `cudaq::qarray<4> q;`（其余代码不动），重新编译应同样通过——这就是 `qarray` 与 `qvector` 的可替换性。

#### 4.2.5 小练习与答案

**练习 1**：`qvector` 的 `operator[]` 返回的是「比特的拷贝」还是「比特的引用」？为什么这样设计？

**答案**：返回的是引用（`value_type &operator[](...)`，见 [qvector.h:L73](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qvector.h#L73)）。因为 `qudit` 不可拷贝，根本无法返回拷贝；返回引用才能让 `x(q[0])` 这类调用读到同一个 `id`，并把门指令正确记录到对应编号上。

**练习 2**：下面这段旧代码该怎么迁移到推荐类型？

```cpp
cudaq::qreg q(5);          // 动态，5 个比特
cudaq::qreg<3> r;          // 编译期，3 个比特
```

**答案**：动态的 `qreg(5)` 迁移为 `cudaq::qvector q(5);`；编译期的 `qreg<3>` 迁移为 `cudaq::qarray<3> r;`。依据正是 `qreg` 的废弃说明（[qreg.h:L37-L39](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qreg.h#L37-L39)）。

---

### 4.3 非拥有视图：qview 与（废弃的）qspan，以及不可拷贝约束

#### 4.3.1 概念说明

容器解决了「拥有」，但带来了一个新问题：**怎么把「一组比特」借给另一个内核或子例程，而不触发拷贝？** 比如主内核分了 5 个比特，想把「后 2 个」交给一个子内核处理。如果用 `qvector` 按值传参，会触发拷贝（已 `= delete`）；按引用传 `qvector&` 虽然可行，但语义上「子内核只关心其中一段」这件事没表达出来。

答案就是**非拥有视图** `qview`：它内部持有一个 `std::span<qudit>`，**不分配任何比特**，只是借用别人已经申请好的编号。因为不拥有，所以：

- 构造/析构不调用 `allocateQudit/returnQudit`，不会扰动编号账本。
- **可以拷贝**——拷贝一个视图只是复制一段指针，并不复制量子态，因此不违反不可克隆定理。

`qspan` 是 `qview` 的旧版本，已被废弃，推荐统一用 `qview`。

#### 4.3.2 核心流程

视图的典型用法是一条「拥有 → 切片 → 借用」链：

```
拥有者：qvector q(n)        （拥有 n 个比特，持有编号）
   │  q.slice(s, k)         （返回 qview，不分配）
   ▼
视图：qview v = q.slice(s,k) （借用 [s, s+k) 这 k 个比特的编号）
   │  把 v 传给子内核或对其作用门
   │  （门函数遍历 v，对每个借来的编号记录指令）
   ▼
v 离开作用域                 （析构什么都不做，编号仍归 q 所有）
   ...
q 离开作用域                 （此时才真正 returnQudit 全部 n 个编号）
```

要点是：**视图的生命周期必须短于拥有者**（dangling 与经典 `std::span`/`string_view` 同理）。视图负责「借」，拥有者负责「生灭」。

#### 4.3.3 源码精读

`qview` 的实现极其轻量——核心就是一个 `std::span<qudit>`（[runtime/cudaq/qis/qview.h:L18-L34](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qview.h#L18-L34)）：

```cpp
/// The `qview` represents a non-owning container of qudits.
template <std::size_t Levels = 2>
class qview {
public:
  using value_type = qudit<Levels>;
private:
  std::span<value_type> qudits;
public:
  template <typename R>
    requires(std::ranges::range<R>)
  qview(R &&other) : qudits(other.begin(), other.end()) {}
```

那个接受任意 range 的构造函数，让 `qview` 能从 `qvector`、`qarray`、甚至另一个 `qview` 构造——只要对方是个可遍历的 qudit 序列。注意它**没有删除拷贝构造**，反而显式提供了拷贝构造（[runtime/cudaq/qis/qview.h:L36-L37](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qview.h#L36-L37)）：

```cpp
/// Copy constructor
qview(qview const &other) : qudits(other.qudits) {}
```

对比 `qvector.h:L58` 的 `qvector(qvector const &) = delete;`，差别一目了然：**拥有型不可拷贝，视图型可拷贝**。这正是本讲「不可拷贝约束」的精确边界——它只约束拥有型类型，不约束视图。

`qview` 也提供 `operator[]`、`slice/front/back`、`begin/end`、`size()`，接口与 `qvector` 几乎一致（[runtime/cudaq/qis/qview.h:L40-L67](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qview.h#L40-L67)），所以你可以对视图再切片、再索引：

```cpp
value_type &operator[](const std::size_t idx) { return qudits[idx]; }
qview<Levels> front(std::size_t count) { return qudits.first(count); }
qview<Levels> slice(std::size_t start, std::size_t count) {
  return qudits.subspan(start, count);
}
std::size_t size() const { return qudits.size(); }
```

废弃的 `qspan` 结构几乎相同，区别只是它把「编译期大小」也作为模板参数（`qspan<N, Levels>`，内部 `std::span<value_type, N>`），并因此被废弃——`qview` 只保留「能级数」一个模板参数，更简洁（[runtime/cudaq/qis/qspan.h:L31-L46](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qspan.h#L31-L46)）：

```cpp
template <std::size_t N = dyn, std::size_t Levels = 2>
class [[deprecated("The qspan type is deprecated in favor of qview.")]] qspan {
public:
  using value_type = qudit<Levels>;
private:
  std::span<value_type, N> qudits;
public:
  template <typename R>
    requires(std::ranges::range<R>)
  qspan(R &&other) : qudits(other.begin(), other.end()) {}
  qspan(qspan const &other) : qudits(other.qudits) {}
```

最后，把「视图」接到「门」上：门函数对「单比特」与「容器/视图」做重载，靠 `std::ranges::range` 概念区分。例如 `x` 的宏展开里既有接受 `QubitArgs&...` 的版本，也有 `requires(std::ranges::range<QubitRange>)` 的版本（[runtime/cudaq/qis/qubit_qis.h:L138-L166](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qubit_qis.h#L138-L166)），后者会对范围内每个比特各作用一次门。`qvector`、`qarray`、`qview`、`qspan` 都满足 `range`，因此都能直接传给门函数做「广播」或「多控」。

把本节和 4.1、4.2 串起来，就得到了本讲最重要的一张对照（与第 2.2 节的表互为印证，但这里给出**源码依据**）：

| 类型 | 持有方式 | 拷贝构造 | 源码依据 |
|------|----------|----------|----------|
| `qudit` / `qubit` | 拥有单比特 | `= delete` | [qudit.h:L44-L46](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qudit.h#L44-L46) |
| `qvector` | 拥有多比特（动态） | `= delete` | [qvector.h:L57-L64](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qvector.h#L57-L64) |
| `qarray` | 拥有多比特（定长） | `= delete` | [qarray.h:L46-L50](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qarray.h#L46-L50) |
| `qview` | 不拥有（视图） | 允许 | [qview.h:L36-L37](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qview.h#L36-L37) |
| `qspan`（废弃） | 不拥有（视图） | 允许 | [qspan.h:L49](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qspan.h#L49) |

#### 4.3.4 代码实践

**实践目标**：把「主内核拥有比特、子内核借视图处理」这套范式写出来，体会视图如何绕开不可拷贝约束。

**操作步骤**：

1. 新建 `qview_subkernel.cpp`（**示例代码**）：

   ```cpp
   // 示例代码：主内核用 qvector 拥有比特，子内核用 qview 借用其中一段
   #include <cudaq.h>

   // 子内核：接收一段比特的视图，对每个比特作用 h
   __qpu__ void apply_h(cudaq::qview<> qs) {
       h(qs);
   }

   __qpu__ void kernel() {
       cudaq::qvector q(4);          // 拥有 4 个比特

       x(q[0]);                       // 给第 0 位做标记
       apply_h(q.slice(2, 2));        // 把后 2 位「借」给子内核做 H

       mz(q);
   }

   int main() {
       cudaq::sample(kernel).dump();
       return 0;
   }
   ```

2. 编译运行：

   ```bash
   nvq++ qview_subkernel.cpp -o qview_subkernel.x
   ./qview_subkernel.x
   ```

**需要观察的现象**：程序正常编译并运行（说明 `qview` 作为参数能正确传递，没有触发拷贝），输出一个 4 比特分布。分析：比特 0 经 x 后为 \(|1\rangle\)；比特 1 没被动，保持 \(|0\rangle\)；比特 2、3 各被 H 作用，处于 \((|0\rangle+|1\rangle)/\sqrt2\)。

**预期结果**：比特 0 恒 1、比特 1 恒 0，比特 2、3 各约 50% 取 0/1，整体分布在 4 种组合上，各约 25%。

3. 对比实验：把子内核签名从 `cudaq::qview<> qs` 改成 `cudaq::qvector<> qs`（按值传拥有型容器），重新编译。

**预期结果（对比）**：编译失败，根因是 `qvector` 的拷贝构造被 `= delete`（[qvector.h:L58](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qvector.h#L58)）。这正说明了「跨内核传一组比特」要用视图而非拥有型容器。

> 待本地验证：第 3 步的具体报错措辞依编译器版本而异，但「deleted copy constructor of qvector」这一根因是稳定的。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `qview` 允许拷贝，而 `qvector` 不允许？请用「量子不可克隆定理」和「编号账本」两个角度解释。

**答案**：（1）物理角度：拷贝 `qvector` 意味着复制其持有的量子态，违反不可克隆定理；拷贝 `qview` 只是复制一段指针，并不复制任何量子态，所以不违反。（2）工程角度：拷贝 `qvector` 会让两个对象都「以为」自己拥有同一批编号，析构时编号被归还两次，破坏 `ExecutionManager` 的账本；`qview` 析构不调用 `returnQudit`，复制它不影响账本。

**练习 2**：下面代码有什么风险？如何修正？

```cpp
__qpu__ cudaq::qview<> get_tail(cudaq::qvector<> &q) {
    return q.slice(2, 2);
}
```

**答案**：风险在于「视图的生命周期可能超过拥有者」。如果调用方拿到返回的 `qview` 时，源 `qvector` 已经析构，视图就指向已归还的比特编号（悬垂视图），行为未定义。修正方式是确保拥有者存活期覆盖视图使用期——通常的做法是**不把视图跨拥有者生命周期传递**，改成在拥有者仍然存活的作用域内就地使用，例如直接在主内核内 `q.slice(2,2)` 传给子内核调用（如 4.3.4 的示例），而不是用一个返回视图的工厂函数。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个综合任务。

**任务**：写一个内核 `ghz_segment`，用 `qvector` 分配 5 个比特，构造一个「分段 GHZ 态」——前 3 个比特做 GHZ 链（\( |000\rangle+|111\rangle\) 形态），后 2 个比特单独做 Bell 态，最后整体测量。要求：

1. 用「单比特索引 + 受控门」实现前 3 比特的 GHZ 链（H 在 q[0]，CNOT q[0]→q[1]，CNOT q[1]→q[2]）。
2. 用「子区间视图」取出后 2 个比特 `q.slice(3, 2)`，在视图上做 Bell 态（H 在 view[0]，CNOT view[0]→view[1]）。**把这一步封装成一个接收 `qview` 的子内核**。
3. 用 `cudaq::sample` 采样，打印分布。

**参考实现框架**（**示例代码**，门语义将在 u2-l2 详讲，此处照抄即可）：

```cpp
#include <cudaq.h>

// 子内核：在传入的视图上做 Bell 态
__qpu__ void bell_pair(cudaq::qview<> qs) {
    h(qs[0]);
    x<cudaq::ctrl>(qs[0], qs[1]);
}

__qpu__ void ghz_segment() {
    cudaq::qvector q(5);

    // (1) 前 3 比特 GHZ 链：用单比特索引
    h(q[0]);
    x<cudaq::ctrl>(q[0], q[1]);
    x<cudaq::ctrl>(q[1], q[2]);

    // (2) 后 2 比特 Bell 态：用子区间视图 + 子内核
    bell_pair(q.slice(3, 2));

    mz(q);
}

int main() {
    cudaq::sample(ghz_segment).dump();
    return 0;
}
```

**自检要点**：

- 程序能编译通过，说明 `qview` 跨内核传参、`qvector` 切片、`operator[]` 索引三件事都正确。
- 思考：前 3 比特应只出现 `000` 和 `111` 两种结果且各约 50%；后 2 比特应只出现 `00` 和 `11` 两种且各约 50%。整体 5 比特分布应集中在 4 种组合（`00000`、`00011`、`11100`、`11111`），每种约 25%。
- 进阶：把 `cudaq::qvector q(5);` 换成 `cudaq::qarray<5> q;`，验证定长拥有者同样可用；再把子内核参数换成按值 `qvector`，验证它**应当编译失败**，从而确认你对不可拷贝约束的理解。

> 待本地验证：上述概率分析基于理想无噪声状态向量后端（默认 qpp），实际采样会有统计涨落；若指定带噪声后端则分布会偏离，相关机制在 u6-l3 讲解。

## 6. 本讲小结

- CUDA-Q 的量子值类型以 `qudit<Levels>` 为根，`qubit = qudit<2>`；每个 qudit 持有一个不可变的全局编号 `idx`，构造时向 `ExecutionManager` 申请、析构时归还。
- 拥有型类型（`qudit`、`qvector`、`qarray`、`qreg`）负责比特的「生灭」，因此**删除了拷贝与移动**——这是量子不可克隆定理在 C++ 类型系统里的直接体现。
- `qvector`（动态）与 `qarray<N>`（定长）是推荐的拥有型容器，分别对应 `std::vector` 与 `std::array`；`qreg` 已废弃，按大小动态/定长迁移到这两者。
- 非拥有视图 `qview`（以及废弃的 `qspan`）内部是 `std::span<qudit>`，不分配比特、可拷贝，专用于「把一组比特借给子内核或子区间操作」。
- 容器的 `slice/front/back` 一律返回 `qview`，复用了 C++ 标准库的 span 机制；视图的生命周期必须短于拥有者。
- 「不可拷贝」只约束拥有型，不约束视图——这是判断某段代码能否编译的关键边界。

## 7. 下一步学习建议

本讲只解决了「量子比特怎么表达、怎么合法地组合与传递」。接下来：

- **u2-l2 量子门与修饰符：ctrl、neg 与自定义作用**：本讲的 `x<cudaq::ctrl>(...)`、`qview` 上的门广播都只是「用了」，下一讲将深入门函数模板、`ctrl/neg/adj` 修饰符如何把单比特门升级为受控门/负控门/逆门。
- **u2-l3 测量与采样：mz/mx/my 与 SampleResult**：本讲反复出现的 `mz` 和 `sample` 的结果结构将在下一讲展开。
- **延伸阅读**：如果想看「这些量子类型如何在 MLIR 里被表示」，可以提前翻阅 [runtime/cudaq/qis/qudit.h](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qudit.h) 与编译器侧的 Quake 方言定义（u4-l2），体会「C++ 类型 ↔ Quake alloca/veq 操作」的对应关系——这会解释为什么 `qvector` 在内核里最终被翻译成 Quake 的值语义向量。
