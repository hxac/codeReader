# 单元测试框架：test/UnitTest

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 LLVM-libc **为什么要自己写一套测试框架**（而不是直接用 GoogleTest）。
- 用 `TEST` / `TEST_F` / `TYPED_TEST` 三种宏把一段断言代码注册成一个可被框架发现并运行的测试用例。
- 区分 `EXPECT_*`（记录失败但继续）与 `ASSERT_*`（失败立即返回），并能根据语义选用 `EQ/NE/LT/...`、`STREQ`、`TRUE/FALSE`、`EXPECT_THAT` 等断言。
- 解释测试用例是如何被「发现」的：靠全局静态对象的构造函数把自己挂进一条链表，再由 `main` 驱动遍历。
- 认识三类特殊测试工具：**hermetic（封闭）测试**、**death test（子进程死亡测试）**、以及 **`ErrnoCheckingTest` 夹具**。

## 2. 前置知识

本讲假设你已经学过 [u2-l1 入口点机制](u2-l1-entrypoint-mechanism.md)，知道：

- LLVM-libc 把每个公开函数做成一个独立的构建单元（entrypoint），其点分全限定名形如 `libc.src.ctype.isalpha`。
- 所有内部实现符号都被关进带隐藏可见性的命名空间 `LIBC_NAMESPACE`（展开后是带版本后缀的 `__llvm_libc`），公开 C 符号靠 `LLVM_LIBC_FUNCTION` 宏的 asm 别名映射出来。

此外需要一点 C++ 基础：

- **静态对象的构造**：一个全局/静态对象的构造函数会在 `main` 之前执行，本讲的测试注册正是利用这一点。
- **纯虚函数与虚析构**：框架的基类 `Test` 有纯虚 `Run()`，理解「子类 override 后通过基类指针调用」即可。
- **gtest 风格**：你不必真的用过 GoogleTest，但知道 `TEST(Suite, Name) { ... }` 这种写法会很有帮助——LLVM-libc 的这套框架就是在刻意模仿它。

一个关键直觉：**测试代码本身也要 link 进一个可执行文件，而 LLVM-libc 的目标环境（GPU、baremetal）往往没有 C++ 标准库、甚至没有系统 libc**。所以测试框架必须是「自包含」的，这正是它一切设计取舍的根源。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [test/UnitTest/Test.h](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/UnitTest/Test.h) | 统一入口头：在自带框架 / gtest / zxtest 三者间做选择，定义若干字符串工具宏。 |
| [test/UnitTest/LibcTest.h](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/UnitTest/LibcTest.h) | 自带框架的核心：`Test` 基类、`TestCond` 枚举、`RunContext`、所有 `TEST`/`EXPECT_*`/`ASSERT_*` 宏定义。 |
| [test/UnitTest/LibcTest.cpp](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/UnitTest/LibcTest.cpp) | 核心实现：`test_impl` 比较与诊断、`addTest` 链表、`runTests` 主循环、各类型的模板显式实例化。 |
| [test/UnitTest/LibcTestMain.cpp](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/UnitTest/LibcTestMain.cpp) | 程序入口 `main`：解析命令行参数后调用 `Test::runTests`。 |
| [test/src/ctype/isalpha_test.cpp](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/src/ctype/isalpha_test.cpp) | 范例测试：用 `TEST` + `EXPECT_EQ/NE` 检验 `isalpha`。 |
| [test/UnitTest/ErrnoSetterMatcher.h](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/UnitTest/ErrnoSetterMatcher.h) | 自定义匹配器范例：`Succeeds` / `Fails` 同时断言返回值与 `errno`。 |
| [test/UnitTest/ErrnoCheckingTest.h](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/UnitTest/ErrnoCheckingTest.h) | 测试夹具：每个用例前后自动清零/校验 `errno`。 |
| [test/UnitTest/ExecuteFunction.h](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/UnitTest/ExecuteFunction.h) | death test 的基础设施：在子进程里执行一个可调用对象并汇报其退出/信号状态。 |
| [test/UnitTest/LibcDeathTestExecutors.cpp](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/UnitTest/LibcDeathTestExecutors.cpp) | death test 的实现：`testProcessExits` / `testProcessKilled`。 |
| [test/UnitTest/HermeticTestUtils.cpp](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/UnitTest/HermeticTestUtils.cpp) | hermetic 测试的运行时支撑：自带的 `malloc`/`free` 与内存函数转发。 |
| [test/src/unistd/_exit_test.cpp](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/src/unistd/_exit_test.cpp) | death test 范例：用 `EXPECT_EXITS` 验证 `_exit`。 |

## 4. 核心概念与源码讲解

### 4.1 测试宏：TEST / TEST_F / TYPED_TEST 与 EXPECT_* / ASSERT_*

#### 4.1.1 概念说明

写一个测试，本质上是「定义一个类、把一段断言代码放进它的 `Run()` 方法、再让框架能找到它」。这三件事在 gtest 里被 `TEST(Suite, Name)` 一个宏包办。LLVM-libc 的自带框架提供与 gtest 几乎同名的三组宏：

- `TEST(SuiteName, TestName)`：最常用，定义一个独立的测试用例。
- `TEST_F(SuiteClass, TestName)`：带夹具（fixture），用例继承自一个你事先写好的 `SuiteClass`，可复用 `SetUp`/`TearDown` 与成员变量。
- `TYPED_TEST(SuiteName, TestName, TypeList)`：类型参数化，把同一份测试逻辑对一组类型（如 `float`/`double`/`long double`）各实例化一次——数学库测试大量使用它。

配套的断言宏分两个家族：

- `EXPECT_*`：**非致命**断言。失败时记录一次失败、打印诊断，但**继续执行**当前用例的后续代码。
- `ASSERT_*`：**致命**断言。失败时**立即从当前用例函数 `return`**，适合「后面的话都依赖这条成立」的场景（比如先断言指针非空，再解引用）。

#### 4.1.2 核心流程

一个测试用例从写出到运行，经历以下步骤：

1. 你写 `TEST(LlvmLibcIsAlpha, SimpleTest) { ... }`。
2. 预处理期，`TEST` 宏展开成一个类 `LlvmLibcIsAlpha_SimpleTest`（继承 `Test`），并声明一个**全局静态实例** `LlvmLibcIsAlpha_SimpleTest_Instance`。
3. 程序加载时、`main` 之前，该静态实例的构造函数执行 `addTest(this)`，把自己挂进全局链表。
4. `main` 调用 `Test::runTests`，遍历链表，对每个用例依次 `SetUp()` → `Run()` → `TearDown()`。

要点：**你不需要「显式注册」测试**——全局对象的构造替你完成了注册。这也是 gtest 的做法。

#### 4.1.3 源码精读

先看统一入口 [test/UnitTest/Test.h:L31-L38](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/UnitTest/Test.h#L31-L38)，它在三套后端之间选择：

```cpp
#if defined(LIBC_COPT_TEST_USE_ZXTEST)
#include "ZxTest.h"
#elif defined(LIBC_COPT_TEST_USE_GTEST) || defined(LIBC_COPT_TEST_USE_PIGWEED)
#include "GTest.h"
#else
#include "LibcTest.h"   // 默认：用 LLVM-libc 自带的框架
#endif
```

默认走 `LibcTest.h`。注释 [test/UnitTest/Test.h:L25-L29](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/UnitTest/Test.h#L25-L29) 说明：三套后端都提供 `EXPECT_*`/`ASSERT_*`，并把 `LIBC_NAMESPACE::testing::Test` 定义为夹具基类。所以**测试源码只 `#include "test/UnitTest/Test.h"`，换后端不用改测试代码**。

`TEST` 宏的定义在 [test/UnitTest/LibcTest.h:L388-L398](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/UnitTest/LibcTest.h#L388-L398)：

```cpp
#define TEST(SuiteName, TestName)                                              \
  static_assert(valid_prefix(#SuiteName), "必须以 'LlvmLibc' 开头");            \
  class SuiteName##_##TestName : public LIBC_NAMESPACE::testing::Test {        \
  public:                                                                      \
    SuiteName##_##TestName() { addTest(this); }   // 构造即注册                  \
    void Run() override;                                                        \
    const char *getName() const override { return #SuiteName "." #TestName; }   \
  };                                                                           \
  SuiteName##_##TestName SuiteName##_##TestName##_Instance;  // 全局静态实例      \
  void SuiteName##_##TestName::Run()   // 你的 { ... } 接在这里
```

注意三件事：

1. **命名约束**：开头那句 `static_assert(valid_prefix(...))` 强制 Suite 名必须以 `LlvmLibc` 打头（见 [test/UnitTest/LibcTest.h:L251-L253](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/UnitTest/LibcTest.h#L251-L253) 的 `valid_prefix`），用于统一命名、避免冲突。
2. **`addTest(this)`**：构造函数里把自己交给框架（注册机制详见 4.3）。
3. **末尾的 `Instance`**：一个具名的全局对象，它的存在仅仅是「让构造函数被调用一次」。

断言宏层面，所有二元比较最终都落到同一个 `LIBC_TEST_BINOP_` 脚手架。先看脚手架 [test/UnitTest/LibcTest.h:L431-L443](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/UnitTest/LibcTest.h#L431-L443)：

```cpp
#define LIBC_TEST_SCAFFOLDING_(TEST, RET_OR_EMPTY)                             \
  LIBC_TEST_DISABLE_DANGLING_ELSE                                              \
  if (TEST) ;                                                                  \
  else RET_OR_EMPTY LIBC_NAMESPACE::testing::internal::Failure() =             \
      LIBC_NAMESPACE::testing::internal::Message()

#define LIBC_TEST_BINOP_(COND, LHS, RHS, RET_OR_EMPTY)                         \
  LIBC_TEST_SCAFFOLDING_(                                                       \
      LIBC_NAMESPACE::testing::internal::test(                                 \
          LIBC_NAMESPACE::testing::TestCond::COND, LHS, RHS, ...),             \
      RET_OR_EMPTY)
```

`RET_OR_EMPTY` 是这套设计的点睛之笔：传 `return` 就是 `ASSERT_*`（失败即返回），传空就是 `EXPECT_*`（失败继续）。后面的 `Failure() = Message()` 还顺手支持了 `EXPECT_EQ(a, b) << "附加说明"` 这种链式打日志的写法。具体的宏列表在 [test/UnitTest/LibcTest.h:L448-L464](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/UnitTest/LibcTest.h#L448-L464)：

```cpp
#define EXPECT_EQ(LHS, RHS) LIBC_TEST_BINOP_(EQ, LHS, RHS, )
#define ASSERT_EQ(LHS, RHS) LIBC_TEST_BINOP_(EQ, LHS, RHS, return)
// ... NE / LT / LE / GT / GE 同理
```

此外还有布尔与字符串两族：`EXPECT_TRUE/EXPECT_FALSE` 被归约为 `EXPECT_EQ(VAL, true/false)`（[L469-L473](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/UnitTest/LibcTest.h#L469-L473)）；`EXPECT_STREQ/STRNE` 走专门的 `test_str_eq/test_str_ne`（[L482-L492](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/UnitTest/LibcTest.h#L482-L492)）。

一个值得注意的设计决定写在 [test/UnitTest/LibcTest.h:L38-L43](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/UnitTest/LibcTest.h#L38-L43)：`TestCond` 枚举**故意没有 `TRUE`/`FALSE`**——因为 C 库函数不返回布尔值，而是用「非零表示真」的整型约定，直接比 `!= 0` 更贴合语义（`isalpha` 测试正是这么写的）。

最后看真实用法 [test/src/ctype/isalpha_test.cpp:L33-L42](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/src/ctype/isalpha_test.cpp#L33-L42)：

```cpp
TEST(LlvmLibcIsAlpha, SimpleTest) {
  EXPECT_NE(LIBC_NAMESPACE::isalpha('a'), 0);   // 字母 → 非零
  EXPECT_NE(LIBC_NAMESPACE::isalpha('B'), 0);

  EXPECT_EQ(LIBC_NAMESPACE::isalpha('3'), 0);   // 非字母 → 0
  EXPECT_EQ(LIBC_NAMESPACE::isalpha(' '), 0);
  EXPECT_EQ(LIBC_NAMESPACE::isalpha(-1), 0);    // 负值边界
}
```

注意调用写的是 `LIBC_NAMESPACE::isalpha(...)`——测试直接对**内部命名空间下的函数**断言，而不是公开 C 符号 `::isalpha`。这样做既能绕开与系统 libc 同名符号的冲突，又能精确测试 LLVM-libc 自己的实现。

#### 4.1.4 代码实践

**目标**：亲手感受「全局静态实例 = 自动注册」这一机制。

1. 阅读 [test/UnitTest/LibcTest.h:L388-L398](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/UnitTest/LibcTest.h#L388-L398) 中 `TEST` 宏的定义。
2. 在脑中（或用 `clang -E` 预处理）把 `TEST(LlvmLibcIsAlpha, SimpleTest)` 展开成类 + 全局实例。
3. 把 Suite 名故意改成 `MyIsAlpha`（不带 `LlvmLibc` 前缀），预测会发生什么。

**需要观察的现象**：第 3 步会在编译期触发 `static_assert` 失败，报错信息正是宏里写的那句 `"All LLVM-libc TEST suite names must start with 'LlvmLibc'."`。这验证了命名约束是在**编译期**生效的，而不是运行期。

**预期结果**：编译失败，错误指向 `valid_prefix(#SuiteName)` 那一行。若你想看完整展开，可用（待本地验证）：

```bash
clang++ -E -I <libc源码根> test/src/ctype/isalpha_test.cpp 2>/dev/null | grep -A3 "class LlvmLibcIsAlpha_SimpleTest"
```

#### 4.1.5 小练习与答案

**练习 1**：`EXPECT_EQ(a, b)` 和 `ASSERT_EQ(a, b)` 在宏定义上的唯一区别是什么？

> **答案**：第四个参数 `RET_OR_EMPTY`：`EXPECT_EQ` 传空，`ASSERT_EQ` 传 `return`。失败时前者继续执行当前用例，后者立即从 `Run()` 返回。

**练习 2**：为什么 `TestCond` 枚举里没有 `TRUE`/`FALSE`？

> **答案**：因为 C 标准库函数普遍用「非零整型表示真」而非布尔，用 `NE(x, 0)` 比较更贴合语义；框架作者刻意不提供 `TRUE`/`FALSE` 以免误导。

---

### 4.2 断言匹配器：从 test_impl 到 EXPECT_THAT 与自定义 Matcher

#### 4.2.1 概念说明

4.1 讲的是「比较两个值」的二元断言。但很多场景需要更丰富的判定，比如：

- 「返回值等于 0 **且** `errno` 等于 0」——一次调用要同时断言两件事。
- 「浮点结果在容差范围内」——需要自定义比较逻辑和自定义的错误说明。

为此框架提供了两条扩展路径：

1. **底层比较函数 `test_impl`**：所有 `EXPECT_EQ/NE/...` 最终都调它，它负责真正比较并打印诊断（「Expected: ... Which is: ... To be equal to: ...」）。
2. **匹配器（Matcher）机制 + `EXPECT_THAT`**：把「如何判定」封装成一个对象，匹配失败时由对象自己解释错误。这是 gmock 风格 `EXPECT_THAT(value, matcher)` 的复刻。

#### 4.2.2 核心流程

二元断言的执行链：

1. `EXPECT_EQ(LHS, RHS)` → `LIBC_TEST_BINOP_(EQ, ...)`。
2. 调 `internal::test(TestCond::EQ, LHS, RHS, ...)`，它是一个**模板函数**，按 `LHS`/`RHS` 的类型（整型 / 枚举 / 指针 / 字符串视图）走不同重载，目的是做**强类型检查**——避免不同类型隐式提升后「意外相等」。
3. `test` 转调 `test_impl`，后者用一个 lambda `ExplainDifference` 真正执行比较：成立返回 `true`，不成立则把当前 `RunContext` 标记为失败，并向日志 `tlog` 打印双方值。

`EXPECT_THAT(value, matcher)` 的执行链：

1. 展开为 `matchAndExplain(matcher, value, ...)`（[test/UnitTest/LibcTest.h:L529-L537](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/UnitTest/LibcTest.h#L529-L537)）。
2. 调 `matcher.match(value)` 得到一个 `bool`。
3. 交给 `Test::testMatch`（[test/UnitTest/LibcTest.cpp:L310-L322](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/UnitTest/LibcTest.cpp#L310-L322)）：匹配则通过，否则标记失败并调 `matcher.explainError()` 让匹配器自述原因。

#### 4.2.3 源码精读

先看强类型分派的几个 `test` 重载。整型版本 [test/UnitTest/LibcTest.h:L110-L117](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/UnitTest/LibcTest.h#L110-L117)：

```cpp
template <typename ValType,
          cpp::enable_if_t<cpp::is_integral_v<ValType> || is_big_int_v<ValType> ||
                               cpp::is_fixed_point_v<ValType>,
                           int> = 0>
bool test(TestCond Cond, ValType LHS, ValType RHS, ...) {
  return test_impl(current_context, Cond, LHS, RHS, LHSStr, RHSStr, Loc);
}
```

注释 [test/UnitTest/LibcTest.h:L103-L109](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/UnitTest/LibcTest.h#L103-L109) 解释了为何要用「`LHS`、`RHS` 作显式模板参数」而非「传一个已算好的 `bool`」：避免类型不匹配时因隐式提升而「假相等」。指针版本 [test/UnitTest/LibcTest.h:L126-L132](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/UnitTest/LibcTest.h#L126-L132) 还特化了一个与 `nullptr` 比较的重载，以支持 `ASSERT_EQ(foo, nullptr)`。

真正的比较与诊断在 `test_impl` 里 [test/UnitTest/LibcTest.cpp:L91-L124](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/UnitTest/LibcTest.cpp#L91-L124)：

```cpp
auto ExplainDifference = [=, &Ctx](bool Cond, cpp::string_view OpString) -> bool {
  if (Cond) return true;
  Ctx->markFail();                    // 标记当前用例失败
  tlog << Loc;                         // 文件名:行号: FAILURE
  tlog << "Expected: " << LHSStr << '\n'
       << "Which is: " << describeValue(LHS) << '\n'
       << "To be " << OpString << ": " << RHSStr << '\n'
       << "Which is: " << describeValue(RHS) << '\n';
  return false;
};
switch (Cond) {
  case TestCond::EQ: return ExplainDifference(LHS == RHS, "equal to");
  case TestCond::NE: return ExplainDifference(LHS != RHS, "not equal to");
  // ... LT/LE/GT/GE
}
```

`RunContext` 本身非常薄 [test/UnitTest/LibcTest.h:L86-L95](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/UnitTest/LibcTest.h#L86-L95)：只持有一个 `Pass/Fail` 状态字。每个用例运行时栈上新建一个 `RunContext`，通过全局指针 `current_context`（[LibcTest.cpp:L36](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/UnitTest/LibcTest.cpp#L36)）让断言宏能回写结果。

> 这里有个常被忽视的细节：`test_impl` 是模板，必须在头文件声明、在 `.cpp` 里**显式实例化**所有要支持的类型。看 [test/UnitTest/LibcTest.cpp:L234-L290](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/UnitTest/LibcTest.cpp#L234-L290) 的 `TEST_SPECIALIZATION` 列表——从 `char` 到 `unsigned long long`，再到 `UInt<128>`、`cpp::string` 等都一一列出。这就是为什么 `EXPECT_EQ` 能比较 128 位整数和字符串。

匹配器一侧，基类 [test/UnitTest/LibcTest.h:L45-L54](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/UnitTest/LibcTest.h#L45-L54) 只规定接口：

```cpp
struct MatcherBase {
  virtual ~MatcherBase() {}
  virtual void explainError() { tlog << "unknown error\n"; }
  virtual bool is_silent() const { return false; }
};
template <typename T> struct Matcher : public MatcherBase {
  bool match(const T &t);   // 子类提供具体判定
};
```

最实用的自定义匹配器范例是 `ErrnoSetterMatcher`，它一次断言「返回值 + errno」两件事。核心 `match` 在 [test/UnitTest/ErrnoSetterMatcher.h:L205-L214](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/UnitTest/ErrnoSetterMatcher.h#L205-L214)：

```cpp
bool match(T got) {
  actual_return = got;
  actual_errno = libc_errno;   // 先抓取当前 errno
  libc_errno = 0;              // 再清零，供后续断言
  if constexpr (ignore_errno())
    return return_cmp.compare(actual_return);
  else
    return return_cmp.compare(actual_return) &&
           errno_cmp.compare(actual_errno);
}
```

它的工厂函数 `Succeeds` / `Fails` 用起来很直观（用法见文件头注释 [L12-L31](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/UnitTest/ErrnoSetterMatcher.h#L12-L31)）：

```cpp
EXPECT_THAT(LIBC_NAMESPACE::close(fd), Succeeds(0));          // 返回0且errno=0
EXPECT_THAT(LIBC_NAMESPACE::read(-1, buf, 1), Fails(EBADF));  // 返回-1且errno=EBADF
EXPECT_THAT(LIBC_NAMESPACE::socketpair(-1,-1,-1,sv),
            Fails(any_of(EINVAL, EAFNOSUPPORT)));             // errno 是两者之一
```

注意 `Fails` 默认期望返回值是 `-1`（[L252-L257](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/UnitTest/ErrnoSetterMatcher.h#L252-L257)），`any_of`（[L265-L267](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/UnitTest/ErrnoSetterMatcher.h#L265-L267)）则处理「不同内核/QEMU 下 errno 可能不同」的真实场景。这正好承接了 [u4-l3 错误处理](u4-l3-error-handling-errno.md) 讲的「内核返回负 errno → 入口点翻译成返回 -1 + 设 errno」的端到端约定。

#### 4.2.4 代码实践

**目标**：用 `EXPECT_THAT` + `Succeeds`/`Fails` 表达「一次调用同时校验返回值与 errno」。

1. 打开 [test/UnitTest/ErrnoSetterMatcher.h:L12-L31](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/UnitTest/ErrnoSetterMatcher.h#L12-L31) 阅读四种用法注释。
2. 在仓库里找一个用到它的真实测试（例如 `test/src/unistd` 下的 `close`/`read` 测试）对照阅读。
3. 思考：如果用裸 `EXPECT_EQ`，要几行才能等价表达 `Fails(EBADF)`？

**需要观察的现象**：`Fails(EBADF)` 一行等价于「`EXPECT_EQ(ret, -1)` + `EXPECT_EQ((int)libc_errno, EBADF)` + 事后清零 `libc_errno`」三步。匹配器把这套样板封装成了一个可读的整体，失败时还能在 `explainError()`（[L181-L203](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/UnitTest/ErrnoSetterMatcher.h#L181-L203)）里分别说清「是返回值错了还是 errno 错了」。

**预期结果**：你会得出结论——对「带 errno 副作用」的函数，匹配器比裸断言更安全（不易忘记清零 errno）、更易读。本步骤为源码阅读型实践，运行验证待本地进行。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `internal::test` 要为整型、枚举、指针分别写重载，而不是统一收成一个「传 `bool` 结果」的函数？

> **答案**：为了让编译器在 `LHS`、`RHS` 类型不一致时报错/警告，避免隐式类型提升导致「本不该相等却因截断/提升而相等」的假阳性（详见头文件注释 L103-L109）。

**练习 2**：`ErrnoSetterMatcher::match` 为什么在抓取 `actual_errno` 后立刻把 `libc_errno = 0`？

> **答案**：避免上一次调用残留的 errno 污染后续断言；同时让测试作者不必每次手动清零，这是「带 errno 副作用」测试的卫生约定。

---

### 4.3 测试注册与运行：全局链表、runTests、main 与 CMake add_libc_test

#### 4.3.1 概念说明

4.1 提到「构造函数调 `addTest(this)` 完成注册」。本模块把这条链彻底讲清：注册的数据结构是一条**单向链表**，运行就是遍历这条链表。整条链由 `Test` 类的两个静态指针 `Start`/`End` 维护，驱动入口是 `Test::runTests`，再往上由 `main` 调用。构建侧，`add_libc_test` 这条 CMake 规则把你的 `.cpp` 编进一个可执行测试目标。

#### 4.3.2 核心流程

```
[main 启动前]
  每个全局 Instance 的构造函数 → addTest(this)
  → 挂到 Start/End 单链表

[main 运行]
  parseOptions(argc, argv)            // 解析颜色/过滤/计时
  Test::runTests(Options)
    遍历 Start..End:
      新建 RunContext Ctx; current_context = &Ctx
      T->SetUp(); T->Run(); T->TearDown()
      按 Ctx.status() 打印 [ OK ] / [ FAILED ]
    汇总 PASS/FAIL 计数，返回退出码
```

命令行方面，框架模仿 gtest：最后一个非 `--gtest_*` 参数被当作**测试名过滤器**（只跑名字完全匹配的那一个用例），`--gtest_color=no` 关闭颜色，`--gtest_print_time` 改用毫秒计时。

#### 4.3.3 源码精读

链表的插入逻辑 [test/UnitTest/LibcTest.cpp:L137-L146](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/UnitTest/LibcTest.cpp#L137-L146)：

```cpp
void Test::addTest(Test *T) {
  if (End == nullptr) { Start = T; End = T; return; }
  End->Next = T;
  End = T;
}
```

`Test` 类本身见 [test/UnitTest/LibcTest.h:L187-L236](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/UnitTest/LibcTest.h#L187-L236)：它持有私有的 `Test *Next`、静态头尾指针 `Start`/`End`（[L234-L235](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/UnitTest/LibcTest.h#L234-L235)），暴露 `runTests`（[L197](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/UnitTest/LibcTest.h#L197)）与 `addTest`（[L200](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/UnitTest/LibcTest.h#L200)），并有虚的 `SetUp`/`TearDown`/`Run`/`getName` 供子类 override。注释 [L185-L186](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/UnitTest/LibcTest.h#L185-L186) 提醒：不要手工 new `Test` 调方法，一律用 `TEST`/`TEST_F` 宏。

主循环 `runTests` 在 [test/UnitTest/LibcTest.cpp:L155-L230](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/UnitTest/LibcTest.cpp#L155-L230)，核心片段：

```cpp
for (Test *T = Start; T != nullptr; T = T->Next) {
  if (Options.TestFilter && cpp::string(T->getName()) != Options.TestFilter)
    continue;                       // 过滤
  RunContext Ctx;
  internal::current_context = &Ctx; // 让断言宏能回写
  T->SetUp(); T->Run(); T->TearDown();
  internal::current_context = nullptr;
  switch (Ctx.status()) {           // 据 Pass/Fail 上色打印
    case Fail: ++FailCount; ...
    case Pass: ...
  }
}
return FailCount > 0 || TestCount == 0 ? 1 : 0;   // 退出码
```

`main` 极薄 [test/UnitTest/LibcTestMain.cpp:L54-L60](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/UnitTest/LibcTestMain.cpp#L54-L60)：把 `argc/argv/envp` 存进全局变量供测试使用，然后 `Test::runTests(parseOptions(argc, argv))`。注意 [L48-L52](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/UnitTest/LibcTestMain.cpp#L48-L52) 的 `TEST_MAIN` 宏：宿主环境用普通 `int main`，freestanding（hermetic）环境加 `extern "C"`，因为 C++ 标准在非 freestanding 下不允许给 `main` 加链接说明符。

构建侧，一条测试目标由 `add_libc_test` 创建。看范例 [test/src/ctype/CMakeLists.txt:L13-L21](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/src/ctype/CMakeLists.txt#L13-L21)：

```cmake
add_libc_test(
  isalpha_test
  SUITE   libc-ctype-tests
  SRCS    isalpha_test.cpp
  DEPENDS libc.src.ctype.isalpha      # 被测入口点
)
```

`add_libc_test` 的定义在 [cmake/modules/LLVMLibCTestRules.cmake:L1018-L1047](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/cmake/modules/LLVMLibCTestRules.cmake#L1018-L1047)，它会派生出两个目标：

```cmake
add_libc_unittest(${test_name}.__unit__ ...)              # 普通 unit 测试
add_libc_hermetic(${test_name}.__hermetic__               # 封闭测试
  LINK_LIBRARIES LibcTest.hermetic LibcDeathTestExecutors.hermetic ...)
```

这就是为什么 [u1-l3 构建与运行入门](u1-l3-build-and-run.md) 里提到单测目标名形如 `libc.test.src.ctype.isalpha_test.__unit__`——后缀 `.__unit__` / `.__hermetic__` 正来源于此。`DEPENDS libc.src.ctype.isalpha` 把被测入口点拉进来，体现「测试依赖被测对象」的清晰关系（承接 [u2-l1 入口点机制](u2-l1-entrypoint-mechanism.md) 与 [u2-l3 CMake 规则](u2-l3-cmake-build-rules.md)）。

#### 4.3.4 代码实践

**目标**：跑一个现成的单元测试，并理解目标名是怎么来的。

1. 假设你已按 [u1-l3](u1-l3-build-and-run.md) 完成 runtimes 构建得到 build 目录。
2. 只编译 ctype 的 isalpha 测试目标（待本地验证）：

   ```bash
   ninja -C <build> libc.test.src.ctype.isalpha_test.__unit__
   ```
3. 直接运行该可执行文件，再用名字过滤只跑 `SimpleTest`：

   ```bash
   <build>/.../isalpha_test.__unit__ LlvmLibcIsAlpha.SimpleTest
   ```

**需要观察的现象**：输出形如 gtest 风格的 `[==========]`、`[ RUN ]`、`[ OK ]`，最后给出 `PASS: 2 FAIL: 0`；加过滤参数后只跑一个用例。

**预期结果**：你看到两个用例 `LlvmLibcIsAlpha.SimpleTest` 与 `LlvmLibcIsAlpha.DefaultLocale` 全部通过；若把过滤名写错，会得到 `No tests run. / No matching test ...` 且退出码为 1（对应 `runTests` 末尾 `TestCount == 0 ? 1 : 0`）。本步骤需本地有可用构建产物，具体路径待本地确认。

#### 4.3.5 小练习与答案

**练习 1**：测试用例的「注册」发生在什么时刻？为什么不需要一张中央注册表？

> **答案**：发生在程序加载后、`main` 之前的全局对象构造阶段。每个 `TEST` 宏生成的全局 `Instance` 在构造时调 `addTest(this)` 把自己挂进 `Start`/`End` 链表，因此无需中央注册表，新增用例只需写 `TEST(...)` 即可。

**练习 2**：`add_libc_test(isalpha_test ... DEPENDS libc.src.ctype.isalpha)` 里的 `DEPENDS` 起什么作用？

> **答案**：把被测入口点目标 `libc.src.ctype.isalpha`（及其内部依赖）链进测试可执行文件，既表达构建顺序，又让测试能调用 `LIBC_NAMESPACE::isalpha`。

---

### 4.4 特殊测试工具：hermetic 测试、death test 与 ErrnoCheckingTest 夹具

#### 4.4.1 概念说明

普通 unit 测试链的是系统 libc + 标准 C++ 运行时，验证「算法正确性」足够；但 LLVM-libc 还要验证「用我们自己实现的 libc 启动起来也能跑」——这就需要 **hermetic（封闭）测试**：用 `-nostdlib` 链接，全靠自带 libc 与自带的极简运行时。

另外两类常见需求：

- **death test（死亡测试）**：被测函数会**终止进程**（如 `_exit`）或**触发信号**（如越界访问触发的段错误）。这种用例不能在当前进程里直接跑（会把测试进程本身干掉），必须在**子进程**里执行再观察其结局。
- **ErrnoCheckingTest 夹具**：对「会改 `errno`」的函数，每个用例开始前清零、结束后强制校验 errno 必为 0，逼着测试作者把每一步的 errno 都显式核对。

#### 4.4.2 核心流程

**hermetic 的双构建**：同一份测试源码被编译两次——一次是 `.unit`（普通），一次是 `.hermetic`（封闭）。封闭版用极简的 bump 分配器代替 malloc、用 dummy 实现代替 C++ 运行时的纯虚钩子，确保不引入任何系统依赖。

**death test 的执行**：

```
EXPECT_EXITS([]{ _exit(1); }, 1)
  → 把 lambda 包成 FunctionCaller
  → invoke_in_subprocess(func)   // fork 子进程执行
  → 子进程退出 → get_exit_code() 与期望值比较
EXPECT_DEATH([]{ 触发段错误 }, SIGSEGV)
  → 同样子进程执行 → get_fatal_signal() 与期望信号比较
```

**ErrnoCheckingTest 夹具**：靠 `SetUp`/`TearDown` 钩子在每个用例前后自动清零/校验 errno。

#### 4.4.3 源码精读

hermetic 双构建在 [test/UnitTest/CMakeLists.txt:L14-L26](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/UnitTest/CMakeLists.txt#L14-L26) 的 `add_unittest_framework_library` 中体现——它对每个库一次性造出 `<name>.unit` 与 `<name>.hermetic` 两个静态库：

```cmake
foreach(lib IN ITEMS ${name}.unit ${name}.hermetic)
  add_library(${lib} STATIC EXCLUDE_FROM_ALL ${TEST_LIB_SRCS} ${TEST_LIB_HDRS})
  ...
endforeach()
```

而 hermetic 版额外的运行时支撑在 [test/UnitTest/HermeticTestUtils.cpp](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/UnitTest/HermeticTestUtils.cpp)。它的核心是「在没有 libc/C++ 运行时的环境里，手工补齐测试框架运行所必需的符号」。例如一个极简的 bump 分配器（[L40-L43](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/UnitTest/HermeticTestUtils.cpp#L40-L43)、[L84-L86](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/UnitTest/HermeticTestUtils.cpp#L84-L86)）：

```cpp
static constexpr uint64_t MEMORY_SIZE = 1 << 20;        // 1 MiB
alignas(ALIGNMENT) static uint8_t memory[MEMORY_SIZE];
static uint8_t *ptr = memory;
...
void *malloc(size_t s) { return aligned_alloc(ALIGNMENT, s); }
void free(void *) { }   // 故意空实现：永不回收
```

注释 [L34-L39](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/UnitTest/HermeticTestUtils.cpp#L34-L39) 解释：hermetic 测试不能用 Scudo（它会拖入大量 libc 依赖），故用一个只发不收的简单分配器凑合。同文件还把编译器可能隐式调用的 `memcpy`/`memset` 等转发到 `LIBC_NAMESPACE::` 版本（[L53-L68](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/UnitTest/HermeticTestUtils.cpp#L53-L68)），并提供 `__cxa_pure_virtual` 的 trap 实现（[L107-L110](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/UnitTest/HermeticTestUtils.cpp#L107-L110)）以支撑框架用到的虚函数。

death test 的宏只在实际需要时才启用（[test/UnitTest/LibcTest.h:L497-L524](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/UnitTest/LibcTest.h#L497-L524)）：

```cpp
#if LIBC_TEST_SUBPROCESS_TESTS
#define EXPECT_EXITS(FUNC, EXIT)  LIBC_TEST_PROCESS_(testProcessExits, FUNC, EXIT, )
#define ASSERT_EXITS(FUNC, EXIT)  LIBC_TEST_PROCESS_(testProcessExits, FUNC, EXIT, return)
...
#ifdef LIBC_TEST_SKIP_DEATH_TESTS
#define EXPECT_DEATH(FUNC, SIG)    // 在不支持的平台退化为空
#else
#define EXPECT_DEATH(FUNC, SIG)    LIBC_TEST_PROCESS_(testProcessKilled, FUNC, SIG, )
#endif
#endif
```

子进程基础设施在 [test/UnitTest/ExecuteFunction.h:L19-L46](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/UnitTest/ExecuteFunction.h#L19-L46)：`FunctionCaller` 是个虚基类（包住任意 lambda），`invoke_in_subprocess` 把它丢到子进程里跑，`ProcessStatus` 汇报「正常退出/退出码/被信号杀死/超时」。

具体比较逻辑在 [test/UnitTest/LibcDeathTestExecutors.cpp:L68-L108](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/UnitTest/LibcDeathTestExecutors.cpp#L68-L108)，`testProcessExits` 拿子进程退出码与期望值比；`testProcessKilled`（[L23-L66](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/UnitTest/LibcDeathTestExecutors.cpp#L23-L66)）拿致命信号与期望信号比。

真实 death test 范例 [test/src/unistd/_exit_test.cpp:L12-L15](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/src/unistd/_exit_test.cpp#L12-L15)：

```cpp
TEST(LlvmLibcUniStd, _exit) {
  EXPECT_EXITS([] { LIBC_NAMESPACE::_exit(1); }, 1);
  EXPECT_EXITS([] { LIBC_NAMESPACE::_exit(65); }, 65);
}
```

`_exit` 会真的终止进程，所以在子进程里跑、再核对退出码，是唯一安全的测法。

最后看 errno 夹具 [test/UnitTest/ErrnoCheckingTest.h:L46-L57](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/UnitTest/ErrnoCheckingTest.h#L46-L57)：

```cpp
class ErrnoCheckingTest : public Test {
public:
  void SetUp() override { Test::SetUp(); libc_errno = 0; }   // 用例开始清零
  void TearDown() override { ASSERT_ERRNO_SUCCESS(); Test::TearDown(); } // 结束必须为0
};
```

它配套的便捷宏 `ASSERT_ERRNO_EQ` / `ASSERT_ERRNO_SUCCESS` / `ASSERT_ERRNO_FAILURE` 在 [L25-L36](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/UnitTest/ErrnoCheckingTest.h#L25-L36)（GPU 目标下退化为空）。用 `TEST_F` 继承它即可让用例自动享受 errno 卫生检查——用法承接 4.1 的夹具概念。注意 GPU 上没有 errno 概念，所以这些宏用 `LIBC_TARGET_ARCH_IS_GPU` 条件编译做了优雅降级。

#### 4.4.4 代码实践

**目标**：读懂一个 death test，并理解它为什么必须在子进程里跑。

1. 打开 [test/src/unistd/_exit_test.cpp:L12-L15](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/src/unistd/_exit_test.cpp#L12-L15)。
2. 沿调用链跟踪：`EXPECT_EXITS` → `LIBC_TEST_PROCESS_` → `testProcessExits`（[LibcDeathTestExecutors.cpp:L68-L108](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/UnitTest/LibcDeathTestExecutors.cpp#L68-L108)）→ `invoke_in_subprocess`。
3. 思考：如果把 `EXPECT_EXITS([]{ _exit(1); }, 1)` 直接换成在当前进程调用 `_exit(1)`，会发生什么？

**需要观察的现象**：直接在当前进程调用 `_exit(1)` 会让测试可执行文件本身立即以退出码 1 结束——`runTests` 的循环、后续用例、PASS/FAIL 汇总统统不会执行。这正是 death test 必须隔离到子进程的根本原因。

**预期结果**：你应能解释「子进程隔离」是 death test 的核心；并注意到 `testProcessExits` 还处理了超时（`timed_out`）和执行错误（`get_error`）两类异常结局。本步骤为源码阅读型实践，运行验证待本地进行（且需 `LIBC_TEST_SUBPROCESS_TESTS` 开启）。

#### 4.4.5 小练习与答案

**练习 1**：hermetic 测试为什么不能用 Scudo 分配器，而要自带一个 bump 分配器？

> **答案**：Scudo 会拖入大量 libc 依赖、且其开发不使用 LLVM-libc 的构建规则，难以在 `-nostdlib` 的封闭环境里完整拉入。hermetic 测试目的是验证「仅靠自带 libc 也能运行」，故用一个只发不收的简单分配器凑合（见 HermeticTestUtils.cpp 注释 L34-L39）。

**练习 2**：`ErrnoCheckingTest::TearDown` 里调用 `ASSERT_ERRNO_SUCCESS()` 的目的是什么？

> **答案**：强制要求用例结束时 errno 为 0。这样一旦被测函数意外设置了 errno 却没被测试核对，夹具就会让用例失败，逼着测试作者显式说明每一个非零 errno，避免「静默污染」。

## 5. 综合实践

把本讲四个模块串起来，完成一次「为一个 ctype 函数写测试并注册」的端到端练习（对照 [u11-l3 贡献新函数](u11-l3-contribute-new-function.md) 的完整流程，这里只聚焦测试侧）：

1. **选函数**：挑 `isdigit`（或 `isxdigit`）作为被测对象。仓库里已有 [test/src/ctype/isdigit_test.cpp](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/src/ctype/isdigit_test.cpp) 作为标准答案，**先不要看**，自己写。
2. **写用例**（模块 4.1）：仿照 [isalpha_test.cpp:L33-L42](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/src/ctype/isalpha_test.cpp#L33-L42)，新建一个文件，用 `TEST(LlvmLibcIsDigit, SimpleTest)` 覆盖：
   - 数字（如 `'3'`、`'0'`、`'9'`）→ `EXPECT_NE(..., 0)`；
   - 字母（如 `'a'`、`'B'`）→ `EXPECT_EQ(..., 0)`；
   - 边界值（`' '`、`'?'`、`'\0'`）→ `EXPECT_EQ(..., 0)`；
   - 负值（`-1`）→ `EXPECT_EQ(..., 0)`。
3. **加全量扫描用例**（模块 4.2 的强类型断言思想）：仿照 [isalpha_test.cpp:L44-L54](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/src/ctype/isalpha_test.cpp#L44-L54) 的 `DefaultLocale`，用一个 `cpp::span` 装 `0..9`，循环 `ch = -255..255` 验证「在 span 里则非零，否则为零」。
4. **注册到 CMake**（模块 4.3）：在 `test/src/ctype/CMakeLists.txt` 里照 [L13-L21](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/src/ctype/CMakeLists.txt#L13-L21) 加一段 `add_libc_test(isdigit_test ... DEPENDS libc.src.ctype.isdigit)`（若已存在则跳过）。
5. **对照标准答案**：打开 `isdigit_test.cpp` 比对你的版本，检查断言风格、命名前缀、扫描区间是否一致。
6. **反思**（模块 4.4）：ctype 函数不涉及 errno、不终止进程，所以用普通 `TEST` + `.unit__` 即可，不需要 `ErrnoCheckingTest` 夹具，也不需要 death test。说清楚「为什么这个函数不需要这些特殊工具」。

**预期结果**：你产出的测试源码在风格上与仓库现有 ctype 测试一致；能解释每个模块（宏、匹配器、注册、特殊工具）在你这个具体例子里「用到/没用到」的理由。运行验证待本地构建后进行。

## 6. 本讲小结

- LLVM-libc **自带一套 gtest 风格的轻量测试框架**（`test/UnitTest`），因为 gtest 依赖 C/C++ 标准库，会与「正在实现的 libc」本身冲突；同一份测试源码通过 `test/UnitTest/Test.h` 可在三套后端（自带 / gtest / zxtest）间切换。
- **`TEST`/`TEST_F`/`TYPED_TEST`** 宏展开成「继承 `Test` 的类 + 全局静态实例」，靠静态对象的构造函数调 `addTest` 完成自动注册；Suite 名必须以 `LlvmLibc` 打头，否则编译期 `static_assert` 失败。
- **`EXPECT_*`（继续）与 `ASSERT_*`（返回）** 的唯一差别是宏的第四参数 `RET_OR_EMPTY`；所有二元比较最终经强类型模板 `test` → `test_impl`，并刻意不设 `TRUE`/`FALSE` 条件以贴合 C 库「非零即真」的约定。
- **`EXPECT_THAT(value, matcher)`** 提供 gmock 风格扩展点；`ErrnoSetterMatcher` 的 `Succeeds`/`Fails` 是范例，一次调用同时断言返回值与 `errno`。
- **测试运行**靠 `Test::Start/End` 单链表 + `runTests` 主循环 + `main` 驱动；CMake 侧 `add_libc_test` 为每个测试派生 `.__unit__`（普通）与 `.__hermetic__`（封闭，`-nostdlib`）两个目标。
- **三类特殊工具**：hermetic 测试用自带 bump 分配器与运行时补丁验证「仅靠自带 libc 也能跑」；death test（`EXPECT_EXITS`/`EXPECT_DEATH`）在子进程里执行会终止进程或触发信号的函数；`ErrnoCheckingTest` 夹具自动在每个用例前后清零/校验 errno。

## 7. 下一步学习建议

- 想看「类型参数化测试」的真实大规模用法，建议阅读数学库测试（如 `test/src/math/round_test.cpp`），它们用 `TYPED_TEST` + `FPMatcher` 把同一套断言对 `float`/`double`/`long double` 各跑一遍，自然过渡到 [u10-l2 数学正确性验证：MPFR/MPC 高精度对照](u10-l2-math-correctness-mpfr-mpc.md)。
- 想深入了解 death test 的子进程机制，可读 `test/UnitTest/ExecuteFunctionUnix.cpp`（`fork`/`waitpid` 的具体实现）。
- 若你打算贡献新函数，请接着学 [u11-l3 贡献一个完整新函数：端到端实战](u11-l3-contribute-new-function.md)，把本讲的测试编写与 YAML 规范、CMake 注册、`entrypoints.txt` 串成一次完整贡献。
