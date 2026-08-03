# 错误处理模型概览

## 1. 本讲目标

本讲是 orc-rt 错误处理体系的「入门全景课」。学完后你应该能够：

- 理解 `orc_rt::Error` 表示「成功或失败」，`orc_rt::Expected<T>` 表示「值或错误」。
- 理解它们是**通过返回值传递**的「一等值」，而非 C++ 异常——这让 orc-rt 在关闭异常（`-fno-exceptions`）时也能工作。
- 牢记核心契约：**每个 `Error` / `Expected<T>` 在析构前必须被「检查」，否则程序 `abort()`**；这条契约从物理上杜绝了「静默吞掉错误」的 bug。
- 学会一套常用工具：`make_error`、`consumeError`、`handleAllErrors`、`toString`、`cantFail`，并知道它们各自的使用场景。

本讲只建立**用法与心智模型**。指针位的二进制编码、`handleErrors` 的可变参数分派机制、异常互操作等「内部实现」留到进阶讲义（u9）展开。

---

## 2. 前置知识

阅读本讲前，建议你已经具备：

- C++ 基础：类、模板、移动语义（`std::move`）、`std::unique_ptr`。
- 对「返回值表示错误」这种风格的直观认识（类似 Rust 的 `Result` 或 C 的错误码，但更强）。
- 最好已读过 [u1-l1 项目定位](u1-l1-project-overview.md)，知道 orc-rt 是 ORC JIT 的执行端运行时。

### 两种错误的区分

orc-rt 官方策略文档 [docs/ErrorHandling.md:19-28](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/docs/ErrorHandling.md#L19-L28) 把错误分成两类，理解这个区分非常重要：

| 错误类别 | 例子 | 处理方式 |
| --- | --- | --- |
| **可恢复错误（Recoverable）** | 文件读失败、网络问题、畸形输入、找不到符号 | 用 `Error` / `Expected<T>` 返回给调用方 |
| **程序性错误（Programmatic）** | 违反 API 契约、不该出现的空指针、非法枚举值 | 用断言 `assert`，立即终止 |

`Error` / `Expected<T>` **只**用于「可恢复错误」。文档还特别强调一个库设计原则（[docs/ErrorHandling.md:30-35](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/docs/ErrorHandling.md#L30-L35)）：

> **orc-rt 是一个库，绝不能在遇到可恢复错误时调用 `exit()`、`abort()` 或 `std::terminate()`。库应当总是把错误返回给调用者，由应用决定如何处理。**

注意：这里说的「库不能主动 abort」与下文「析构未检查错误会 abort」并不矛盾——后者是**调用方违反契约**（拿到了错误却不处理）时由类型自身触发的保护机制，相当于一个增强的 `assert`。

---

## 3. 本讲源码地图

本讲主要围绕以下文件，都属于 `orc-rt/` 仓库内：

| 文件 | 作用 |
| --- | --- |
| [include/orc-rt/Error.h](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Error.h) | `Error`、`Expected<T>` 与全部错误工具的头文件，本讲的主战场 |
| [docs/ErrorHandling.md](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/docs/ErrorHandling.md) | 官方错误处理策略文档，讲设计原则与推荐用法 |
| [lib/executor/Error.cpp](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/Error.cpp) | 少量实现：`ExceptionError::toString` 与 C ABI 入口函数 |
| [include/orc-rt-c/Error.h](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt-c/Error.h) | C ABI 边界，把 `Error` 暴露成不透明指针 `orc_rt_ErrorRef` |
| [test/unit/ErrorTest.cpp](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/ErrorTest.cpp) | 单元测试，是理解用法与「会触发 abort 的反例」的最佳参考 |

> 提示：orc-rt 的错误体系改编自 LLVM 的 `llvm/Support/Error.h`。如果你读过 LLVM 主仓的 `Error`，会发现 API 几乎一致。

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

- **4.1 `Error`：表示「成功或失败」的值类型**
- **4.2 `Expected<T>`：表示「值或错误」**
- **4.3 「错误必须被检查」契约**
- **4.4 错误的创建、消费与常用辅助函数**

### 4.1 Error：表示「成功或失败」的值类型

#### 4.1.1 概念说明

`Error` 是一个**轻量值类型**，只回答一个问题：某个操作成功了吗？

- **成功**：`Error::success()`，是一个零开销的空值（不分配内存、不构造对象）。
- **失败**：携带一个「类型化的错误载荷」（payload），它是 `ErrorInfoBase` 的某个子类实例（比如 `StringError`）。

它**不是异常**，而是通过**函数返回值**传递的普通对象。这种「返回值式错误」有两个直接好处：

1. **与异常解耦**：即便整个项目用 `-fno-exceptions` 编译，错误照样能正常传递。绝大多数 LLVM 子项目（以及 orc-rt 的默认配置）都不用异常。
2. **强制可见**：错误出现在函数签名和调用点的返回值里，调用方「假装没看见」比忽略异常更难。

`Error` 类被标记了 `[[nodiscard]]`（见源码 4.1.3），编译器会警告你「丢弃了返回值」。注意它的语义约定：

```
explicit operator bool()   // 返回 true = 失败，返回 false = 成功
```

也就是说 `if (auto E = foo())` 里 `E` 为真意味着**出错了**——和很多人直觉里的「真=成功」相反，要特别留意。

#### 4.1.2 核心流程

一个典型的 `Error` 生命周期：

```text
foo() 返回 Error
   │
   ├─ 成功：Error::success()
   │      └─ 调用方 if (!E) { /* 没错，继续 */ }
   │
   └─ 失败：make_error<SomeError>(...)
          └─ 调用方 if (E) { /* 处理或上抛 */ }
                 ├─ 上抛：  return E;            // 交给上一层
                 ├─ 消费：  consumeError(std::move(E));
                 ├─ 处理：  handleAllErrors(std::move(E), [](...){});
                 └─ 转字符串：toString(std::move(E));
```

无论走哪条路，**都必须在 `E` 析构前完成**，否则触发 `abort`（见 4.3）。

#### 4.1.3 源码精读

`Error` 类的声明在 [include/orc-rt/Error.h:85-193](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Error.h#L85-L193)，注意类上的 `[[nodiscard]]`：

```cpp
class [[nodiscard]] Error {
```

最关键的几个成员：

- 析构时强制检查——[include/orc-rt/Error.h:97-99](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Error.h#L97-L99)：`~Error() noexcept { assertIsChecked(); }`。这是整个契约的「执法点」。
- 构造一个成功值——[include/orc-rt/Error.h:131-132](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Error.h#L131-L132)：`static Error success() noexcept { return Error(); }`。注意默认构造的 `Error()` 就是成功值，内部存的是一个空指针。
- 「检查」操作——[include/orc-rt/Error.h:135-138](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Error.h#L135-L138)：

  ```cpp
  explicit operator bool() noexcept {
    setChecked(getPtr() == nullptr);
    return getPtr() != nullptr;
  }
  ```

  返回值由「有没有载荷」决定（有载荷=失败），同时副作用是把「已检查」标记按当前状态写回。

- 携带失败载荷的私有构造——[include/orc-rt/Error.h:155-159](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Error.h#L155-L159)：把一个 `unique_ptr<ErrorInfoBase>` 释放成裸指针存进 `ErrPtr`。`ErrPtr` 是唯一的存储字段（[Error.h:192](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Error.h#L192)），它**同时**编码了「载荷指针」和「是否已检查」两个信息（最低位作标志位，详见 4.3 与 u9）。

- 不可拷贝、只能移动——[include/orc-rt/Error.h:101-102](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Error.h#L101-L102)：`Error(const Error &) = delete;`。一个错误只能被一个 `Error` 对象拥有，避免「同一错误被消费两次」。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：确认「`operator bool` 的真值语义」。

**操作步骤**：

1. 打开 [test/unit/ErrorTest.cpp:66-69](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/ErrorTest.cpp#L66-L69) 的 `CheckedSuccess` 测试。
2. 阅读这行断言：`EXPECT_FALSE(E)`——成功值在布尔上下文里是 `false`。
3. 再看 [test/unit/ErrorTest.cpp:74-80](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/ErrorTest.cpp#L74-L80) 的 `ConsumeError`：`if (E) { ... }`，注释里明确写 `Error failure value should convert to true`。

**需要观察的现象**：失败值转 `true`、成功值转 `false`。

**预期结果**：你会看到两处测试都依赖这个「相反」语义，确认这不是笔误而是设计。

**待本地验证**：若已按 u1-l2 构建出 `check-orc-rt-unit`，可运行 `ErrorTest.CheckedSuccess` 与 `ErrorTest.ConsumeError` 亲自确认。

#### 4.1.5 小练习与答案

**练习 1**：下面代码合法吗？为什么。

```cpp
Error open() { return Error::success(); }
void f() { open(); }   // 忽略返回值
```

**答案**：不合法（实践上会出问题）。虽然 `[[nodiscard]]` 只产生**编译警告**，但这个成功值 `E` 从未被检查，析构时会触发 `assertIsChecked()` → `abort`。正确写法是 `if (auto E = open()) /* 处理 */;` 或 `cantFail(open());`（见 4.4）。

**练习 2**：`if (auto E = foo()) { ... }` 的大括号里，`E` 代表成功还是失败？

**答案**：代表**失败**。`operator bool` 在有载荷（失败）时返回 `true`，所以进入 `if` 体意味着「出错了」。

---

### 4.2 Expected<T>：表示「值或错误」

#### 4.2.1 概念说明

`Expected<T>` 回答的问题比 `Error` 多一个维度：**操作要么返回一个 `T` 值，要么返回一个 `Error`**。它是 `Error` 的「带值版本」，常用于「需要返回结果、但又可能失败」的函数，例如查找符号、解析配置、读取页大小等。

它的内部用 `union` 复用存储——同一时刻**只持有值或错误二者之一**，不会同时分配两份内存。和 `Error` 一样：

- 标记了 `[[nodiscard]]`，不能丢弃。
- **必须被检查**，否则析构 `abort`（见 4.3）。
- 不可拷贝、只能移动。

最易踩的坑是布尔语义**与 `Error` 相反**：

```
Error          : operator bool() == true  → 失败
Expected<T>    : operator bool() == true  → 成功（持有 T）
```

记忆口诀：`Expected` 转 `bool` 问的是「拿到值了吗」；`Error` 转 `bool` 问的是「出错了吗」。

#### 4.2.2 核心流程

典型用法分两步走——**先检查，再分支取值/取错误**：

```text
Expected<Data> R = loadData(path);
   │
   ├─ if (R)  → 成功：用 *R 或 R-> 访问值
   │
   └─ else    → 失败：用 R.takeError() 取出 Error，再按 4.4 的方式处理
```

失败分支里，`R.takeError()` 会把内部载荷「转移」成一个 `Error` 返回给你，并让 `R` 进入可安全析构的「空」状态。文档 [docs/ErrorHandling.md:122-141](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/docs/ErrorHandling.md#L122-L141) 给出了两种等价写法（`takeError` 模式与 `if/else` 模式）。

#### 4.2.3 源码精读

`Expected<T>` 是模板类，声明在 [include/orc-rt/Error.h:359-558](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Error.h#L359-L558)。

- **从值构造（成功态）**——[include/orc-rt/Error.h:389-394](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Error.h#L389-L394)：只要能隐式转换成 `T`，就能直接 `Expected<int> E = 7;`，内部用 `new (getStorage())` 在 union 里就地构造值。

- **从 Error 构造（失败态）**——[include/orc-rt/Error.h:376-379](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Error.h#L376-L379)：`Expected(Error Err)`，且用 `assert(Err && ...)` 保证**不能用成功值**来构造（那在语义上无意义）。它从 `Err` 里 `takePayload()` 取出载荷。

- **检查**——[include/orc-rt/Error.h:433-436](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Error.h#L433-L436)：

  ```cpp
  explicit operator bool() noexcept {
    Unchecked = HasError;
    return !HasError;
  }
  ```

  返回 `!HasError`，所以「有值」时为 `true`。

- **取错误**——[include/orc-rt/Error.h:452-455](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Error.h#L452-L455)：`takeError()` 在成功态返回 `Error::success()`（空操作），在失败态把内部载荷移出，返回一个携带载荷的 `Error`。

- **复用存储**——[include/orc-rt/Error.h:551-557](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Error.h#L551-L557)：一个 `union` 里有 `TStorage` 和 `ErrorStorage` 两个字符数组缓冲，外加两个位域 `HasError`、`Unchecked`。

#### 4.2.4 代码实践（阅读型）

**实践目标**：理解「成功取值、失败取错误」两条路径。

**操作步骤**：

1. 阅读 [test/unit/ErrorTest.cpp:311-316](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/ErrorTest.cpp#L311-L316) 的 `CheckedExpectedInSuccessMode`：`Expected<int> A = 7;`，`EXPECT_TRUE(!!A)`，`EXPECT_EQ(*A, 7)`。这是成功路径。
2. 阅读 [test/unit/ErrorTest.cpp:365-371](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/ErrorTest.cpp#L365-L371) 的 `ExpectedInFailureMode`：从 `make_error<CustomError>(42)` 构造 `A`，`EXPECT_FALSE(!!A)`，再用 `A.takeError()` 取出 `Error` 并 `consumeError`。这是失败路径。

**需要观察的现象**：成功态用 `*A` 取值；失败态必须先 `takeError()` 取出错误再去消费它。

**预期结果**：两条路径都「先检查、再用」，没有一个测试在没检查前就访问 `*A`。

#### 4.2.5 小练习与答案

**练习 1**：`Expected<int> E = make_error<StringError>("oops");` 之后，`*E` 会怎样？

**答案**：会触发断言失败。`E` 处于失败态（`HasError == true`），`operator*` 里调用了 `assertIsChecked()`，且 `getStorage()` 自带 `assert(!HasError && "Cannot get value when an error exists!")`（见 [Error.h:523-526](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Error.h#L523-L526)）。正确做法是先 `if (!E) consumeError(E.takeError());`。

**练习 2**：怎么把一个 `Expected<T>` 的失败向上传递？

**答案**：在失败分支 `return E.takeError();`。因为 `Expected<T>` 有接受 `Error` 的构造函数，返回类型为 `Expected<T>` 的函数可以直接返回一个 `Error`，编译器会把它构造成失败态的 `Expected<T>`。

---

### 4.3 「错误必须被检查」契约

#### 4.3.1 概念说明

这是 orc-rt 错误模型的**心脏**，一句话：

> **每个 `Error` / `Expected<T>` 在析构前都必须被「检查」(checked)，否则 `abort()`。**

为什么要这么严？因为「忘记处理错误」是 C/C++ 里最常见、最难查的 bug 之一。传统的返回码（`int`、`bool`）可以被随手丢弃而毫无声响。orc-rt 用「析构即检查」把这件事变成**运行期硬约束**：你拿到一个错误却不去管它，程序当场崩溃，逼你正面处理。

注意检查的两种含义要分清：

1. **检查（check）**：用 `operator bool`、`takeError()`、`consumeError`、`cantFail` 等方式「看一眼」这个值。这只满足「成功值」的销毁要求。
2. **处理（handle）**：对于**失败值**，光「看一眼」还不够——你必须把它的载荷取走（消费/处理），否则依然 `abort`。换句话说，「检查过但没处理的失败」也会崩溃。

#### 4.3.2 核心流程

`Error` 的析构执法逻辑（`Expected<T>` 同理）：

```text
~Error()
   └─ assertIsChecked()
         ├─ 条件：!isChecked() || getPtr()
         │         ↑ 未检查       ↑ 还有载荷未处理
         └─ 满足任一 → fprintf(stderr,...) + abort()
```

合法的「让一个 Error 安全析构」的方式有三种：

| 方式 | 适用于 | 做了什么 |
| --- | --- | --- |
| `if (E) { ... }` 后续处理 | 成功 / 失败 | 标记已检查；失败仍需进一步处理载荷 |
| `consumeError(std::move(E))` | 失败（已知无害） | 标记已检查并丢弃载荷 |
| `cantFail(std::move(E))` | 已知必定成功 | 标记已检查；若是失败则 `abort` |

#### 4.3.3 源码精读

- `Error` 的析构与执法函数——[include/orc-rt/Error.h:97-99](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Error.h#L97-L99) 与 [include/orc-rt/Error.h:161-166](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Error.h#L161-L166)：

  ```cpp
  void assertIsChecked() noexcept {
    if (ORC_RT_UNLIKELY(!isChecked() || getPtr())) {
      fprintf(stderr, "Error must be checked prior to destruction.\n");
      abort();
    }
  }
  ```

  关键：只有「已检查 **且** 无载荷」才安全。

- `Expected<T>` 的析构——[include/orc-rt/Error.h:423-429](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Error.h#L423-L429)：同样先 `assertIsChecked()`，再按 `HasError` 析构对应的 union 成员。它的 `assertIsChecked` 在 [Error.h:543-549](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Error.h#L543-L549)，报错信息是 `"Expected<T> must be checked before access or destruction."`。

> 进阶提示：`ErrPtr` 如何用一个 `uintptr_t` 同时塞下「指针」和「已检查位」，以及 `operator bool` 如何写回检查位，属于二进制编码细节，留到 u9「Error 内部实现」细讲。本讲只需记住上面的执法条件。

#### 4.3.4 代码实践（阅读死亡测试）

**实践目标**：用测试里的「死亡用例（death test）」直观看到违反契约的后果。

**操作步骤**：阅读 [test/unit/ErrorTest.cpp:83-99](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/ErrorTest.cpp#L83-L99)，其中有两个用例：

1. `UncheckedSuccess`：构造一个 `Error::success()` 却不检查，期望 `EXPECT_DEATH(..., "Error must be checked prior to destruction")`。
2. `CheckedButUnhandledError`：用 `(void)!E;` **检查**了一个失败值，却不消费它，同样期望死亡。

**需要观察的现象**：两种情况都会打印 `"Error must be checked prior to destruction."` 并 `abort`。

**预期结果**：成功值不检查会死；失败值检查了但不处理也会死。

**待本地验证**：死亡测试只在 `NDEBUG` 未定义（debug 构建）时有效；release 构建里 `cantFail(Error)` 的行为会退化为仅复位检查位（见 [Error.h:567-575](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Error.h#L567-L575)）。

#### 4.3.5 小练习与答案

**练习 1**：下面代码合法吗？

```cpp
Error E = make_error<StringError>("boom");
(void)!E;        // 检查
// E 在这里析构
```

**答案**：不合法，会 `abort`。`(void)!E;` 把 `E` 标记为「已检查」，但失败值的载荷还在（`getPtr()` 非空），析构时 `assertIsChecked` 里 `getPtr()` 仍为真 → `abort`。必须 `consumeError(std::move(E));` 才能安全销毁（参考 `CheckedButUnhandledError` 死亡用例）。

**练习 2**：`Error::success()` 不检查也会死，这不是「多此一举」吗？

**答案**：不是。设计者认为**成功路径也必须被显式确认**——否则「函数返回了成功但调用方根本没看」与「函数返回了失败但调用方没看」从外部无法区分。统一要求检查，能逼调用方对每一次返回负责，从根上消灭「静默忽略」。

---

### 4.4 错误的创建、消费与常用辅助函数

#### 4.4.1 概念说明

日常写代码时，你反复用到的就是下面这一组工具。先给一张速查表：

| 函数 / 类型 | 签名要点 | 用途 |
| --- | --- | --- |
| `make_error<ErrT>(args...)` | [Error.h:215-219](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Error.h#L215-L219) | 构造一个 `ErrT` 类型的失败 `Error` |
| `StringError` | [Error.h:604-611](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Error.h#L604-L611) | 最简单的错误类型，只装一个字符串 |
| `ErrorExtends<ThisT, ParentT>` | [Error.h:50-82](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Error.h#L50-L82) | 定义**自定义错误类型**的 CRTP 基类 |
| `consumeError(Error)` | [Error.h:561-563](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Error.h#L561-L563) | **丢弃**一个错误（已知无害时） |
| `handleErrors(E, handlers...)` | [Error.h:318-323](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Error.h#L318-L323) | 按错误类型分派给匹配的 handler，返回未处理的 |
| `handleAllErrors(E, handlers...)` | [Error.h:328-331](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Error.h#L328-L331) | 同上，但**要求全部被处理**，否则 `abort` |
| `toString(Error)` | [Error.h:595-601](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Error.h#L595-L601) | 把错误转成字符串（消费掉错误） |
| `cantFail(E)` / `cantFail(Expected<T>)` | [Error.h:567-583](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Error.h#L567-L583) | 断言「必定成功」并解包值；失败则 `abort` |
| `ErrorAsOutParameter` | [Error.h:335-353](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Error.h#L335-L353) | RAII 守卫，用于「出参 `Error&`」场景 |

**自定义错误类型**：当 `StringError` 不够用时（比如你想携带错误码、文件路径等结构化信息），用 `ErrorExtends` 派生。它内部依赖 orc-rt 自带的 RTTI（不依赖 `-frtti`），所以即便关掉编译器 RTTI 也能做类型判别。文档 [docs/ErrorHandling.md:80-105](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/docs/ErrorHandling.md#L80-L105) 给了一个 `CustomError` 范例，测试里也有真实例子（见 4.4.4）。

> 自定义 RTTI 的原理（`RTTIExtends`、`isa<>`、`classID`）是 u9 的主题，本讲只需知道「它让你能按类型分派错误」即可。

#### 4.4.2 核心流程

**创建并返回一个错误**：

```cpp
Error load(int x) {
  if (x < 0)
    return make_error<StringError>("x must be non-negative");
  return Error::success();
}
```

**消费（丢弃）**：

```cpp
if (auto E = tryPopulateCache(...))
  consumeError(std::move(E));   // 缓存不可用，已知无害
```

**按类型处理**：

```cpp
handleAllErrors(open(path),
  [](const FileNotFoundError &E) { /* 换个路径重试 */ },
  [](const PermissionError    &E) { /* 申请更高权限 */ },
  [](const ErrorInfoBase      &E) { /* 兜底 */ }
);
```

`handleAllErrors` 要求所有错误都被某个 handler 命中；若有遗漏，等于「未处理」，会 `abort`。`handleErrors` 则更宽松——它会返回剩余未处理的 `Error`，适合「处理一部分、其余继续上抛」的场景。

#### 4.4.3 源码精读

- **`make_error` 的两个重载**——[include/orc-rt/Error.h:196-198](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Error.h#L196-L198) 接受现成的 `unique_ptr<ErrorInfoBase>`；[include/orc-rt/Error.h:215-219](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Error.h#L215-L219) 是更常用的变参模板，用 `std::make_unique<ErrT>(args...)` 构造后再交给前者。

- **`StringError`**——[include/orc-rt/Error.h:604-611](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Error.h#L604-L611)：构造时存一个 `std::string`，`toString()` 原样返回。它是「最快得到一个错误」的方式。

- **`consumeError`**——[include/orc-rt/Error.h:561-563](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Error.h#L561-L563)：本质是 `handleAllErrors(std::move(Err), [](const ErrorInfoBase &){});`——用一个什么都不做的 handler 把载荷吃掉。

- **`toString`**——[include/orc-rt/Error.h:595-601](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Error.h#L595-L601)：用 `handleAllErrors` 取出载荷，调用其虚函数 `ErrorInfoBase::toString()`（[Error.h:36](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Error.h#L36)）。注意它会**消费**掉传入的 `Error`。

- **`cantFail`**——[include/orc-rt/Error.h:567-583](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Error.h#L567-L583)：`cantFail(Error)` 在 debug 下若是失败就 `abort`；`cantFail(Expected<T>)` 还会顺手把值取出来返回（`return std::move(*E);`），是「我确信这里不会失败，给我值」的快捷写法。

- **C ABI 入口**——[lib/executor/Error.cpp:57-67](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/Error.cpp#L57-L67)：`orc_rt_Error_consume` / `orc_rt_Error_toString` 等函数把上面的 C++ 工具暴露成 C 接口（用不透明指针 `orc_rt_ErrorRef`），这样 controller 侧的 C 代码也能消费执行端返回的错误。

#### 4.4.4 代码实践（阅读 + 改写型）

**实践目标**：学会「自定义错误 + 按类型处理」的完整闭环。

**操作步骤**：

1. 阅读 [test/unit/ErrorTest.cpp:24-34](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/ErrorTest.cpp#L24-L34) 的 `CustomError`：它 `public ErrorExtends<CustomError, ErrorInfoBase>`，持有一个 `int Info`，并实现 `toString()`。
2. 阅读 [test/unit/ErrorTest.cpp:102-109](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/ErrorTest.cpp#L102-L109) 的 `HandleCustomError`：用 `handleAllErrors(make_error<CustomError>(42), [&](const CustomError &CE){ ... })` 把载荷里的 `42` 取出来。
3. 阅读 [test/unit/ErrorTest.cpp:302-308](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/ErrorTest.cpp#L302-L308) 的 `StringError`：`make_error<StringError>("foo")` 配合 `toString` 得到 `"foo"`。

**需要观察的现象**：handler 的参数类型（`const CustomError &`）决定了它能捕获哪种错误；`handleAllErrors` 会把载荷以正确的动态类型传给匹配的 handler。

**预期结果**：`CaughtErrorInfo` 被正确赋值为 42；`toString` 返回 `"foo"`。

**待本地验证**：把 `HandleCustomError` 里的 `make_error<CustomError>(42)` 换成 `make_error<StringError>("x")`，观察由于没有匹配 `StringError` 的 handler，`handleAllErrors` 会触发 `abort`（因为要求「全部被处理」）。

#### 4.4.5 小练习与答案

**练习 1**：`handleErrors` 和 `handleAllErrors` 有什么区别？什么时候用哪个？

**答案**：`handleErrors` 返回一个 `Error`（剩余未处理的部分），适合「处理一部分、其余继续上抛」，因此它本身返回的 `Error` 仍需被检查；`handleAllErrors` 返回 `void`，要求所有错误都被 handler 命中，否则 `cantFail` 会 `abort`。当你**确信**能处理全部情况时用后者，当你想「兜底处理某些、其余上抛」时用前者。

**练习 2**：为什么 `toString(Error)` 要按值接收 `Error`（而不是 `const Error&`）？

**答案**：因为它要**消费**这个错误（调用 `handleAllErrors`）。错误是只能移动、必须唯一拥有的，按值接收（实际是按 `Error` 移动构造）正好表达「调用方把所有权交给我，我用完就销毁」。接收 `const Error&` 既无法移动载荷，也会让原 `Error` 在调用后仍处于「未处理」状态而触发析构 `abort`。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个完整的小任务（对应本讲的 `practice_task`）。

**任务**：写一个返回 `Expected<int>` 的函数 `square_if_positive`：输入为负时返回 `make_error<StringError>`，否则返回平方值。然后在调用处用 `takeError()` 与 `operator bool` 正确处理两种情况。

**参考实现（示例代码）**：

```cpp
#include "orc-rt/Error.h"
#include <iostream>

using namespace orc_rt;

// 4.4 的 make_error + StringError：失败时返回带信息的错误
Expected<int> square_if_positive(int x) {
  if (x < 0)
    return make_error<StringError>("input must be non-negative");
  return x * x;            // 成功：隐式从 int 构造 Expected<int>
}

int main() {
  // 成功路径：operator bool == true（拿到值）
  if (auto R = square_if_positive(5)) {
    std::cout << "result = " << *R << "\n";   // 预期 25
  } else {
    // 不会进这里；失败也要消费错误以满足契约
    consumeError(R.takeError());
  }

  // 失败路径：operator bool == false（拿到错误）
  if (auto R = square_if_positive(-3)) {
    std::cout << *R << "\n";
  } else {
    // 4.4 的 toString：把错误转成字符串（同时消费它）
    std::cout << "error: " << toString(R.takeError()) << "\n";
    // 预期输出：error: input must be non-negative
  }
}
```

**操作步骤**：

1. 把上面的代码放进一个 `.cpp` 文件，确保能 include 到 `orc-rt/Error.h`（按 u1-l2 构建出 `orc-rt-headers` 后链接）。
2. 编译运行（开启 `ORC_RT_ENABLE_EXCEPTIONS` 与否都可，本例不用异常）。
3. 把 `square_if_positive(-3)` 调用处的 `else` 分支整段删掉，重新编译运行，观察程序因「`R` 未被检查/处理」而 `abort`，并打印 `"Expected<T> must be checked before access or destruction."`。

**需要观察的现象**：

- 正输入打印 `result = 25`。
- 负输入打印 `error: input must be non-negative`。
- 删掉失败处理后会崩溃。

**预期结果**：与上述一致。**待本地验证**：第 3 步的崩溃行为依赖 debug 构建（`NDEBUG` 未定义）。

**进阶（可选）**：仿照 [test/unit/ErrorTest.cpp:24-34](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/ErrorTest.cpp#L24-L34)，把 `StringError` 换成一个自定义的 `NegativeInputError`（携带原始入参值），在调用处用 `handleAllErrors` 按类型捕获并打印入参。

---

## 6. 本讲小结

- `Error` 表示「成功或失败」，`Expected<T>` 表示「值或错误」；二者都是**通过返回值传递的一等值**，不依赖 C++ 异常。
- 成功是零开销的（`Error::success()` 是空值）；失败才携带一个**类型化载荷**（`ErrorInfoBase` 子类）。
- 核心契约：**每个 `Error` / `Expected<T>` 析构前必须被检查，失败值还必须被处理（取走载荷），否则 `abort()`**——这是杜绝「静默吞错」的硬约束。
- 留意相反的布尔语义：`Error` 转 `bool` 为真表示**失败**，`Expected<T>` 转 `bool` 为真表示**成功**。
- 常用工具：`make_error` 建错误，`consumeError` 丢弃，`handleAllErrors` / `handleErrors` 按类型处理，`toString` 转字符串，`cantFail` 断言成功并解包。
- 自定义错误用 `ErrorExtends` 派生，背后是 orc-rt 自带的、不依赖 `-frtti` 的 RTTI。

---

## 7. 下一步学习建议

- **接下来读 u3-l1（Session 对象与构造）**：`Session` 的 `reportError` 就是这套错误体系的第一个真实消费者——你会看到「执行端如何把无法上抛的错误汇报给 Session」。
- **进阶选读 u9（错误处理与 RTTI 深入）**：
  - u9-l1 讲自定义 RTTI（`RTTIExtends`、`isa<>`），解释 `ErrorExtends` 背后的类型判别机制。
  - u9-l2 深挖 `Error` 的指针位编码、`handleErrors` 的可变参数分派等内部实现。
  - u9-l3 讲开启异常时 `Error` 与异常的双向转换（`runCapturingExceptions`、`ExceptionError`）。
- **如果想立刻动手**：把 `test/unit/ErrorTest.cpp` 通读一遍，它是本讲全部概念的可运行佐证；再用 `check-orc-rt-unit` 跑一次，确认绿色。
