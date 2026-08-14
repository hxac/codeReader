# AscendString：跨 ABI 的字符串封装

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚为什么 metadef 的对外头文件不直接暴露 `std::string`，而要封装一个 `ge::AscendString`。
2. 掌握 `AscendString` 的构造（含定长构造）、`GetString` / `GetLength` / `Find` / `Hash` 以及全套比较运算符的用法和边界行为（空对象）。
3. 理解「声明在 `inc/external`、桥接在 `pkg_inc`、实现在 `base`」这一三层结构在字符串这个具体案例上的落地方式。
4. 明白为什么对外接口大量使用 `const char_t *`，以及 `char_t` 这个别名的来历。

## 2. 前置知识

### 2.1 什么是 ABI，为什么字符串会「破坏」它

**ABI**（Application Binary Interface，应用二进制接口）约定了已编译代码之间如何协作：结构体多大、成员偏移在哪、函数符号叫什么名字。上一讲（u2-l1）已经看到，`DataType` / `Format` 枚举的取值顺序就是 ABI 契约，只能尾部追加。

`std::string` 的问题更隐蔽：它的**内部布局**（多大数据内联存储、用什么引用计数、符号是否带 `__cxx11` 前缀）由编译器标准库版本和编译选项决定。典型例子是 GCC 的 `_GLCXX11_ABI` 开关（u1-l4 中示例 Makefile 里的 `-D_GLIBCXX_USE_CXX11_ABI=0` 正是为了与预编译库保持一致）：

- 如果 metadef 的头文件里写 `std::string f();`，那么 metadef、ge、算子仓三方的 `std::string` 布局必须**完全一致**，任何一方换了标准库版本都可能崩溃。
- 反过来，如果头文件只暴露一个「不透明」的类，`std::string` 被藏在实现里，则只有编译 `libmetadef.so` 的那一方需要关心它的布局。

`AscendString` 就是后一种做法。

### 2.2 `char_t` 是什么

在阅读头文件之前先认识一个别名（u2-l1 已提过）：

```cpp
using char_t = char;
```

见 [inc/external/graph/types.h:20](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/types.h#L20)。

它目前就是 `char`，但用别名隔离后，将来若需要切换字符类型（例如宽字符），只需改这一行实现侧定义，所有对外接口签名不变。这就是「接口大量使用 `const char_t *`」的原因之一：**裸 C 指针是所有编译器都认识的通用语言**，跨越 so 边界最安全。

### 2.3 `shared_ptr` 与自定义删除器

`AscendString` 内部用 `std::shared_ptr<std::string>` 持有字符串。`shared_ptr` 允许传入一个**自定义删除器**（deleter）：析构时不用默认的 `delete`，而是调用你给的函数。本讲会看到源码用它解决一个很实际的问题——「在哪个 so 里 `new` 的，就在哪个 so 里 `delete`」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [inc/external/graph/ascend_string.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/ascend_string.h#L1-L70) | 对外声明：`ge::AscendString` 类、构造/查询/比较接口、`std::hash` 特化 |
| [pkg_inc/base/type/ascend_string_impl.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/pkg_inc/base/type/ascend_string_impl.h#L17-L54) | 桥接头：声明 `AscendStringImpl` 静态工具类（实现侧专用，不对外发布） |
| [base/type/ascend_string_impl.cc](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/type/ascend_string_impl.cc#L15-L218) | 全部实现：构造、查询、比较、`Find`、`Hash`，编入 `libmetadef.so` |
| [tests/ut/base/testcase/ascend_string_unittest.cc](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/ascend_string_unittest.cc#L14-L163) | 单元测试：比较运算符、定长构造、内嵌 `\0`、`Find`、`Hash` |

这正是 u1-l3 总结的「声明 → 桥接 → 实现」链路在单个类上的完整样本。

## 4. 核心概念与源码讲解

### 4.1 AscendString 的类声明：对外只露一个壳

#### 4.1.1 概念说明

`AscendString` 要解决的问题：**给上层（ge、算子仓）一个可以放进头文件的字符串类型，同时把 `std::string` 彻底关在 so 内部**。手段是经典的多层封装：

- 对外类 `ge::AscendString` 只有一个私有成员 `std::shared_ptr<std::string> name_`，并且所有成员函数**只声明不定义**（唯一例外是 `std::hash` 特化，见 4.3）。
- 真正操作 `std::string` 的代码全部在 `base/type/ascend_string_impl.cc` 中，编译进 `libmetadef.so`。

于是上层拿到的是：一个大小固定的 `shared_ptr` 成员 + 一组通过 so 符号调用的函数。`std::string` 的布局变化不再影响任何调用方。

`AscendString` 在 metadef 体系里是「名字类字符串」的标准载体：算子名、属性名、图元素名等对外接口都用它（或 `const char_t *`）传递，u1-l4 的示例中我们已经见过它作为跨 ABI 字符串出现。

#### 4.1.2 核心流程

一个 `AscendString` 的生命周期：

```text
用户代码                          libmetadef.so
─────────────────────────────────────────────────────
AscendString s("relu");
    │ 构造函数只是壳
    └──► AscendStringImpl::Construct
              │ new std::string("relu")
              │ 包进 shared_ptr，并挂自定义删除器 Destroy
              ▼
         s.name_ 指向字符串（或 nullptr，如果传了 nullptr）

const char_t *p = s.GetString();
    └──► AscendStringImpl::GetString
              │ name_ 为空 → 返回静态空串 ""
              │ 否则 → 返回 name_->c_str()

s 离开作用域
    └──► 引用计数归零 → 自定义删除器 Destroy → delete
```

#### 4.1.3 源码精读

先看对外声明：

```cpp
class AscendString {
 public:
  AscendString() = default;
  ~AscendString() = default;

  AscendString(const char_t *const name);
  AscendString(const char_t *const name, size_t length);

  const char_t *GetString() const;
  size_t GetLength() const;
  size_t Find(const AscendString &ascend_string) const;
  size_t Hash() const;
  // ... 比较运算符若干
 private:
  std::shared_ptr<std::string> name_;
  friend class AscendStringImpl;
};
```

见 [inc/external/graph/ascend_string.h:22-59](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/ascend_string.h#L22-L59)。

这段声明有三个值得注意的设计：

1. **两个构造函数都接受 `const char_t *`**，没有 `std::string` 版本——这是刻意的，防止 `std::string` 以任何形式出现在对外签名里。带 `length` 的版本用于内容里可能内嵌 `'\0'` 的场景。
2. **`friend class AscendStringImpl;`**：实现类是友元，可以访问私有成员 `name_`。壳类自己一行逻辑都不写。
3. **成员只有 `shared_ptr`**：单个裸指针大小，布局稳定，拷贝即共享同一份字符串（浅拷贝语义，写时不需要复制，因为接口不提供修改操作）。

对应的桥接头声明了纯静态的工具类：

```cpp
class AscendStringImpl {
 public:
  static const char_t *GetString(const AscendString &obj);
  static size_t GetLength(const AscendString &obj);
  static size_t Find(const AscendString &obj, const AscendString &ascend_string);
  static size_t Hash(const AscendString &obj);
  static bool Lt(const AscendString &obj, const AscendString &other);
  // ...
  static void Construct(AscendString &obj, const char_t *const name);
  static void Construct(AscendString &obj, const char_t *const name, size_t length);
  static void Destroy(const std::string *const ptr);
};
```

见 [pkg_inc/base/type/ascend_string_impl.h:18-53](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/pkg_inc/base/type/ascend_string_impl.h#L18-L53)。注意 `AscendStringImpl` 没有成员变量、构造函数都是 `= default`，全部能力以 `static` 函数提供，参数第一位永远是 `AscendString &obj`——它本质上是「写在类外面的成员函数」。

#### 4.1.4 代码实践

**实践目标**：验证「壳类头文件里没有一行逻辑」，并学会从声明定位到实现。

**操作步骤**：

1. 打开 [inc/external/graph/ascend_string.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/ascend_string.h#L22-L59)，确认除 `std::hash` 特化外没有任何成员函数带函数体。
2. 在仓库根目录执行符号查找：

```bash
# 只读检查：libmetadef 的源码侧，确认 AscendString 方法都定义在 .cc 里
grep -n "AscendString::" base/type/ascend_string_impl.cc
```

3. 对比 `Find` 的声明（头文件 L36）与定义（impl.cc L215-217）。

**需要观察的现象**：`grep` 应列出十余行 `AscendString::XXX` 定义，全部集中在 `ascend_string_impl.cc`，头文件里一处都没有。

**预期结果**：确认「声明/实现分离」，且这些符号最终都在 `libmetadef.so` 中（由 u1-l2 讲过的 `base/host.cmake` 编入）。若本机已编译过，可用 `nm build_gcov/ut_base/libmetadef* 2>/dev/null | grep AscendString` 辅助验证——具体路径**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `AscendString` 不提供 `std::string GetString() const;` 而返回 `const char_t *`？

**答案**：返回 `std::string` 会把标准库类型暴露到对外签名，调用方必须与 metadef 使用完全相同布局的 `std::string`，破坏 ABI 隔离；`const char_t *` 是跨 so 的通用类型。代价是返回指针的生命周期归 `AscendString` 管理，调用方不得在原对象销毁后继续使用（本例中 `name_` 是 `shared_ptr`，只要还有引用就不会释放）。

**练习 2**：`AscendString` 的拷贝是深拷贝还是浅拷贝？依据是什么？

**答案**：浅拷贝（共享）。唯一成员 `name_` 是 `std::shared_ptr<std::string>`，默认拷贝构造只复制指针、增加引用计数；类也没有自定义拷贝构造。由于接口不提供任何修改操作，这种共享不可变字符串是安全的。

---

### 4.2 构造与析构：把 new/delete 锁在同一个 so 里

#### 4.2.1 概念说明

跨 so 边界管理内存有个经典陷阱：不同 so（甚至同一 so 的不同编译单元）可能链接**不同的分配器**（例如一个用系统 malloc，一个静态链接了替换库）。如果 A so 里 `new` 的对象拿到 B so 里 `delete`，可能命中不同的堆实现导致崩溃或统计错乱。

metadef 的解法在源码里有一行中文注释直接点明：**「控制对象构造析构在同一个 so 实现」**。

#### 4.2.2 核心流程

```text
Construct(obj, "relu"):
  name == nullptr ?  → 什么都不做，obj.name_ 保持空
  否则:
    ptr = new (nothrow) std::string("relu")     ← 分配发生在 libmetadef.so 内
    obj.name_ = shared_ptr(ptr, 删除器=Destroy)  ← 删除器是本 so 内的函数
    分配失败 → REPORT_INNER_ERR_MSG + GELOGE 记日志

引用计数归零:
  调用删除器 Destroy(ptr) → delete ptr           ← 释放也发生在 libmetadef.so 内
```

注意失败路径：使用 `new (std::nothrow)`，失败返回 `nullptr` 而不是抛 `bad_alloc` 异常——对外库保持异常安全的常用做法。

#### 4.2.3 源码精读

```cpp
// 控制对象构造析构在同一个so实现
void AscendStringImpl::Destroy(const std::string *const ptr) {
  if (ptr != nullptr) {
    delete ptr;
  }
}

void AscendStringImpl::Construct(AscendString &obj, const char_t *const name) {
  if (name != nullptr) {
    obj.name_ = std::shared_ptr<std::string>(new (std::nothrow) std::string(name),
                                             [](const std::string *const ptr) { Destroy(ptr); });
    if (obj.name_ == nullptr) {
      REPORT_INNER_ERR_MSG("E18888", "new string failed.");
      GELOGE(FAILED, "[New][String]AscendStringImpl[%s] make shared failed.", name);
    }
  }
}
```

见 [base/type/ascend_string_impl.cc:16-32](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/type/ascend_string_impl.cc#L16-L32)。

要点：

1. `Destroy` 是 `libmetadef.so` 内的函数，lambda 把它包成删除器。即使上层 so 最后一个释放 `shared_ptr`，真正执行 `delete` 的仍是 metadef 自己的代码。
2. 传 `nullptr` 构造是**合法**的：`name_` 保持空，后续所有查询接口按「空对象」语义处理（见 4.3），不崩溃。
3. 带 `length` 的重载逻辑相同，只是用 `std::string(name, length)` 构造，可以正确保存内嵌 `'\0'` 的内容，见 [base/type/ascend_string_impl.cc:34-43](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/type/ascend_string_impl.cc#L34-L43)。

壳类的构造函数则只有一行转发：

```cpp
AscendString::AscendString(const char_t *const name) {
  AscendStringImpl::Construct(*this, name);
}
```

见 [base/type/ascend_string_impl.cc:163-169](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/type/ascend_string_impl.cc#L163-L169)。

#### 4.2.4 代码实践

**实践目标**：通过阅读现成测试，理解「带 length 构造」与「内嵌 `\0`」两个边界场景。

**操作步骤**：

1. 阅读 [tests/ut/base/testcase/ascend_string_unittest.cc:142-150](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/ascend_string_unittest.cc#L142-L150) 的 `with_terminal` 用例：

```cpp
std::string with_terminal_str("abc\0def", 7);
AscendString with_terminal("abc\0def", 7);
AscendString without_terminal("abc\0def");
ASSERT_EQ(with_terminal.GetLength(), with_terminal_str.length());
ASSERT_GT(with_terminal.GetLength(), without_terminal.GetLength());
```

2. 解释为什么 `with_terminal.GetLength()` 是 7，而 `without_terminal.GetLength()` 是 3。
3. 如本地具备构建环境（u1-l2 的 `tests/run_test.sh -u`），运行该测试观察结果；无环境则记为**待本地验证**。

**需要观察的现象**：`ASSERT_GT` 成立——定长构造保留了 `\0` 之后的 4 个字符，C 风格构造在第一个 `\0` 截断。

**预期结果**：`GetLength` 分别为 7 和 3；`std::string re_build_str(with_terminal.GetString(), with_terminal.GetLength())` 能完整还原 7 字节内容（用 `GetString()` 单独看 C 字符串则只能看到 `abc`）。

#### 4.2.5 小练习与答案

**练习 1**：`AscendString(nullptr)` 之后调用 `GetString()` 会发生什么？

**答案**：`Construct` 对 `nullptr` 直接跳过，`name_` 为空；`GetString` 检测到空指针后返回一个函数内 `static const std::string kEmptyString` 的 `c_str()`，即安全的空串 `""`，见 [base/type/ascend_string_impl.cc:45-51](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/type/ascend_string_impl.cc#L45-L51)。

**练习 2**：为什么 `Construct` 用 `new (std::nothrow)` 而不是普通 `new`？

**答案**：普通 `new` 分配失败会抛出 `std::bad_alloc` 异常，异常跨越 so 边界的展开行为依赖双方一致的运行时；`nothrow` 版本失败返回 `nullptr`，代码随后用 `REPORT_INNER_ERR_MSG` + `GELOGE` 上报错误（u5-l3 将详细讲错误上报），整个调用链保持无异常。

**练习 3**：测试 `null_size`（[ascend_string_unittest.cc:137-140](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/ascend_string_unittest.cc#L137-L140)）构造时传了 `(nullptr, 1)`，断言 `GetLength() == 0`。请解释 length=1 为何没生效。

**答案**：`Construct(obj, name, length)` 的第一行就是 `if (name != nullptr)`，指针为空时无论 length 是多少都不创建字符串，`name_` 保持空，`GetLength` 对空对象固定返回 `0UL`。

---

### 4.3 查询、比较与哈希：空对象语义与 std::hash 特化

#### 4.3.1 概念说明

这一组接口要回答三个问题：

1. **空对象（`name_ == nullptr`）如何表现？** metadef 给出了明确定义：空对象等于空串、哈希等于 `std::hash<std::string>("")`、与任何非空对象比较时「最小」，`Find` 返回 `std::string::npos`。所有分支在实现里逐一显式处理，不依赖未定义行为。
2. **比较运算符怎么实现？** 六个运算符 `< > <= >= == !=` 加上与 `const char_t *` 比较的两个重载，共八个，每个都先分类空/非空四种组合再比较。
3. **怎么放进哈希容器？** 在 `std` 命名空间为 `ge::AscendString` 特化 `std::hash`，使其能直接作为 `std::unordered_map` 的 key。

#### 4.3.2 核心流程

各接口对空对象的处理一览（`obj.name_ == nullptr` 记为「空」）：

| 接口 | obj 为空 | 对象非空 |
| --- | --- | --- |
| `GetString()` | 返回静态 `""` | `name_->c_str()` |
| `GetLength()` | `0` | `name_->length()` |
| `Hash()` | `std::hash("")`（静态缓存） | `std::hash(*name_)` |
| `Find(sub)` | `std::string::npos`（sub 空也是 npos） | `name_->find(*sub.name_)` |
| `obj == other` | 双空 `true`；单空 `false` | 逐字符比较 |
| `obj < other` | 双空 `false`；obj 空 `true` | 字典序比较 |

哈希一致性：`Hash()` 直接复用 `std::hash<std::string>`，因此 `AscendString("abcd").Hash() == std::hash<std::string>()("abcd")`——测试 `hash` 用例正是断言这一点。

#### 4.3.3 源码精读

查询三件套（空对象安全）：

```cpp
const char_t *AscendStringImpl::GetString(const AscendString &obj) {
  if (obj.name_ == nullptr) {
    static const std::string kEmptyString = "";
    return kEmptyString.c_str();
  }
  return obj.name_->c_str();
}
```

见 [base/type/ascend_string_impl.cc:45-58](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/type/ascend_string_impl.cc#L45-L58)（`GetString`）与 [base/type/ascend_string_impl.cc:53-58](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/type/ascend_string_impl.cc#L53-L58)（`GetLength`）。

哈希使用函数内 static 缓存空串哈希值，避免每次重算：

```cpp
size_t AscendStringImpl::Hash(const AscendString &obj) {
  if (obj.name_ == nullptr) {
    static const size_t kEmptyStringHash = std::hash<std::string>()("");
    return kEmptyStringHash;
  }
  return std::hash<std::string>()(*(obj.name_));
}
```

见 [base/type/ascend_string_impl.cc:60-66](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/type/ascend_string_impl.cc#L60-L66)。

`Find` 的空对象语义是「任一侧为空都找不到」：

```cpp
size_t AscendStringImpl::Find(const AscendString &obj, const AscendString &ascend_string) {
  if ((obj.name_ == nullptr) || (ascend_string.name_ == nullptr)) {
    return std::string::npos;
  }
  return obj.name_->find(*(ascend_string.name_));
}
```

见 [base/type/ascend_string_impl.cc:156-161](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/type/ascend_string_impl.cc#L156-L161)。

与 `const char_t *` 比较的重载使用 `strcmp`，见 [base/type/ascend_string_impl.cc:136-154](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/type/ascend_string_impl.cc#L136-L154)。

最后是对外头文件里唯一的「有函数体」的代码——`std::hash` 特化：

```cpp
namespace std {
template <>
struct hash<ge::AscendString> {
  size_t operator()(const ge::AscendString &name) const {
    return name.Hash();
  }
};
}  // namespace std
```

见 [inc/external/graph/ascend_string.h:62-69](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/ascend_string.h#L62-L69)。

它必须在头文件里定义（`std::hash` 特化属于接口契约的一部分，每个使用方都要能看到），但它只调用 `name.Hash()` 这个 so 内函数，仍然不触碰 `std::string` 布局，ABI 安全。有了它，上层可以直接写：

```cpp
// 示例代码：仅演示用法
std::unordered_map<ge::AscendString, int32_t> op_name_to_id;
```

#### 4.3.4 代码实践

**实践目标**：为 `AscendString` 补充「从 `std::string` 构造并 `Find` 子串」的测试用例，走一遍 metadef 的单测贡献流程。

**操作步骤**：

1. `Read` 打开 [tests/ut/base/testcase/ascend_string_unittest.cc](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/ascend_string_unittest.cc#L152-L162)，先精读已有的 `find` 与 `hash` 两个用例作为模板。
2. 在文件末尾（`}  // namespace ge` 之前）新增一个用例，参考写法（**示例代码**，需自行创建/修改测试文件，注意本教程规定不改动源码仓库，建议在本地副本或分支上操作）：

```cpp
TEST_F(UtestAscendString, find_from_std_string) {
  // 场景：从 std::string 出发构造 AscendString，再查找子串
  std::string data = "conv2d_weight_layout";
  AscendString full(data.c_str());            // std::string -> const char_t* -> AscendString
  AscendString sub1("weight");
  AscendString sub2("pooling");
  const size_t pos = full.Find(sub1);
  ASSERT_NE(pos, std::string::npos);          // 能找到
  ASSERT_EQ(pos, 7U);                         // 出现在下标 7
  ASSERT_EQ(full.Find(sub2), std::string::npos);  // 找不到返回 npos
  ASSERT_EQ(full.Hash(), std::hash<std::string>()(data));  // 哈希与同内容 std::string 一致
}
```

3. 由于 `tests/ut/base/CMakeLists.txt` 用 glob 自动收集 `testcase/*.cc`（u1-l2 已讲），直接向**已有文件**追加用例无需改 CMake。
4. 运行（需先 `source` CANN 环境变量，见 u1-l2）：

```bash
bash tests/run_test.sh -u
# 或只跑这一个用例（构建完成后）：
# ./build_gcov/ut_base/ut_metadef --gtest_filter=UtestAscendString.find_from_std_string
```

**需要观察的现象**：新增用例被执行且 `PASSED`；三条断言分别验证「构造自 `std::string`」「命中位置正确」「未命中返回 `npos`」。

**预期结果**：全部通过。若本地没有完整 CANN 构建环境，此步骤**待本地验证**；此时可先做纯阅读版实践——人工比对 `std::string("conv2d_weight_layout").find("weight")` 的结果与断言值 `7U` 是否一致（可由 `"conv2d_".length() == 7` 推出）。

#### 4.3.5 小练习与答案

**练习 1**：`AscendString a("abc"); AscendString b; a.Find(b)` 返回什么？为什么？

**答案**：返回 `std::string::npos`。默认构造的 `b` 其 `name_` 为空，`Find` 实现里 `ascend_string.name_ == nullptr` 分支直接返回 `npos`（[base/type/ascend_string_impl.cc:156-161](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/type/ascend_string_impl.cc#L156-L161)）。注意这并不等于「空串是所有串的子串」的数学语义（`std::string("abc").find("")` 其实返回 0），metadef 选择了更保守的空对象语义。

**练习 2**：如果去掉 `std::hash<ge::AscendString>` 特化，哪些代码会受影响？

**答案**：所有把 `AscendString` 当作 `std::unordered_map` / `std::unordered_set` key 的代码将无法编译（无默认哈希可用）；改用 `std::map`（需要 `operator<`）不受影响，因为比较运算符仍然完整。

**练习 3**：对比 `AscendStringImpl::Lt`（[base/type/ascend_string_impl.cc:68-78](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/type/ascend_string_impl.cc#L68-L78)）和 `Le`（L104-112）：两者对「obj 为空」的分支结论一致吗？

**答案**：一致，都返回 `true`（空对象视为最小，`空 < 任何非空`、`空 <= 任何对象` 都成立）。但两者对「other 为空」的处理路径不同：`Lt` 中 obj 非空时进入 `*obj.name_ < *other.name_` 会先解引用空指针——实际上 `Lt` 用 else-if 链保证走不到该分支（other 为空已在前一分支返回 `false`），而 `Le` 直接先判断 `obj.name_ == nullptr` 返回 `true`。阅读时要注意这类分支覆盖的完备性：每个函数必须把「双空 / 单空×2 / 双非空」四种组合都处理完。

## 5. 综合实践

**任务：给 `AscendString` 做一次「空对象行为全扫描」测试并整理成表格。**

要求：

1. 在本地分支上打开 [tests/ut/base/testcase/ascend_string_unittest.cc](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/ascend_string_unittest.cc#L14-L163)，新增一个测试用例 `empty_object_semantics`，覆盖下表中每一格对应的行为：

| 表达式 | 空对象 `e` 与 非空 `s("abc")` | 预期 |
| --- | --- | --- |
| `e.GetString()` | 返回 `""` | `ASSERT_STREQ(e.GetString(), "")` |
| `e.GetLength()` | 0 | `ASSERT_EQ` |
| `e.Hash()` | 等于 `std::hash<std::string>()("")` | `ASSERT_EQ` |
| `e == AscendString()` | `true` | `ASSERT_TRUE` |
| `e < s` / `s < e` | `true` / `false` | `ASSERT_TRUE/FALSE` |
| `e == "abc"` / `e != "abc"` | `false` / `true` | `ASSERT_EQ` |
| `e.Find(s)` / `s.Find(e)` | 双向 `npos` | `ASSERT_EQ(..., std::string::npos)` |

2. 用 `bash tests/run_test.sh -u`（或 `--gtest_filter=UtestAscendString.empty_object_semantics`）运行验证；无构建环境时，逐行对照 [base/type/ascend_string_impl.cc](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/type/ascend_string_impl.cc#L45-L161) 的分支人工推演每格结果，并标注「待本地验证」。
3. 推演过程中若发现某格的预期值说不清依据（哪一分支），回到 4.3.2 的表格重新核对。

这个任务把本讲三个模块串起来：空对象语义（4.2/4.3）、比较与哈希（4.3）、以及 metadef 的测试流程（u1-l2 的 run_test.sh）。

## 6. 本讲小结

- `AscendString` 存在的根本原因是 **ABI 隔离**：`std::string` 的布局随标准库版本/编译选项变化，不能出现在对外头文件里；对外只暴露 `const char_t *`（`char_t` 是 [types.h:20](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/types.h#L20) 的别名）和一个不透明壳类。
- 三层结构与 u1-l3 的总结完全对应：声明在 [inc/external/graph/ascend_string.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/ascend_string.h#L22-L59)，桥接在 [pkg_inc/base/type/ascend_string_impl.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/pkg_inc/base/type/ascend_string_impl.h#L18-L53)，实现编入 `libmetadef.so`（[base/type/ascend_string_impl.cc](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/type/ascend_string_impl.cc#L15-L218)）。
- 构造使用 `new (std::nothrow)` + 自定义删除器 `Destroy`，把分配和释放都锁在同一个 so 内，并保持无异常的失败路径。
- 空对象有完整定义的语义：`GetString` 返回 `""`、`GetLength` 为 0、`Hash` 等于空串哈希、`Find` 返回 `npos`、比较时视为最小值——所有查询接口都不会因空对象崩溃。
- `std::hash<ge::AscendString>` 特化（头文件里唯一有函数体的代码）让它可以直接作为 `unordered_map` 的 key，且哈希值与同内容 `std::string` 一致。
- 带 `length` 的构造重载可保存内嵌 `'\0'` 的内容，测试 `with_terminal` 验证了这一点。

## 7. 下一步学习建议

下一讲（u2-l3）将学习 `AnyValue` 与 `TypeId`：算子属性系统需要一个能装下 int、float、`vector<string>` 等多种类型的统一容器。你会看到本讲的两个伏笔如何被复用——`AscendString` 是 `AnyValue` 支持的属性类型之一，而 u2-l1 提到的 `GetTypeId<T>` 静态地址方案将在那里正式展开。建议先自行浏览 [pkg_inc/graph/any_value.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/pkg_inc/graph/any_value.h) 和 [pkg_inc/graph/type_id.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/pkg_inc/graph/type_id.h)，留意 `AnyValue` 对 `AscendString` 的处理方式与本讲的空对象语义有何呼应。
