# AnyValue 与 TypeId：类型擦除的属性容器

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `ge::AnyValue` 为什么存在：它如何让一个固定布局的类承载任意类型的值（类型擦除）。
2. 讲解 `TypeId` 体系的设计：为什么常见类型用固定整数做 ID，而自定义类型用静态变量地址做 ID。
3. 掌握 `AnyValue` 的双存储策略（小对象内联 / 大对象堆分配）与函数指针操作表 `operate_` 的分发机制。
4. 安全地用 `SetValue` / `Get<T>` / `GetValue` / `SameType<T>` / `GetValueType` 存取不同类型的值，并正确处理类型不匹配的失败路径。
5. 能仿照 `tests/ut/base/testcase/any_value_ut.cc` 的断言风格，为 AnyValue 写一个可被 `tests/run_test.sh -u` 跑到的自测用例。

## 2. 前置知识

- **类型擦除（type erasure）**：C++ 是静态类型语言，一个成员变量在编译期就确定了类型。但算子属性是「名字 → 任意值」的映射：`axis` 可能是 int，`format` 可能是字符串列表，`mean_rt` 可能是嵌套的命名属性组。要让一个容器装下所有这些，就需要把值的「类型信息」和「数据本体」从编译期搬到运行期——这就是类型擦除。你可能听过的 `std::any`、`std::function` 都是这个思路。
- **函数指针操作表**：类型擦除后，容器自己不知道值是什么类型，拷贝、析构、取地址这些操作都只能委托给「构造值时顺便记下的、针对该类型的函数」。AnyValue 把这些操作收敛成一个统一签名的函数指针 `operate_`，用枚举参数区分要做哪件事——这是手写类型擦除的经典手法。
- **跨 so 的静态变量陷阱**：模板静态成员（如 `TypeIdHolder<T>::id`）定义在头文件里，多个共享库各自包含这个头文件时，若无特殊导出，每个 `.so` 都会有一份自己的实例，**地址不同**。用「地址」当类型 ID 在跨 so 比较时就会失灵。metadef 的解法在 4.1 节详解。
- **ABI 视角（承接 u1-l1、u2-l2）**：AnyValue 位于 `pkg_inc/graph/any_value.h`，属于随 CANN 包发布的头文件。它的成员只有「一个指针大小的联合体 + 一个函数指针」，**布局与所存值的类型无关**，这正是它能在 ABI 边界上安全传递的原因——对比 `std::string`（内部布局随库版本变化，见 u2-l2），这个约束解释了它为什么这样设计。
- **前置讲义**：本讲依赖 u2-l1 中「`GetTypeId<T>` 以静态变量地址做类型 ID」的伏笔，以及 u1-l3 的「头文件（inc/pkg_inc）声明 → base 实现」三层结构。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [pkg_inc/graph/any_value.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/pkg_inc/graph/any_value.h) | `AnyValue` 类的完整定义。既是声明也是实现（模板成员全部内联在此），是实现含量最高的一个发布头文件 |
| [pkg_inc/graph/type_id.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/pkg_inc/graph/type_id.h) | `TypeId` 类型别名、通用 `GetTypeId<T>` 模板，以及对 22 种常用类型的显式特化**声明** |
| [base/any_value.cc](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/any_value.cc) | 上述 22 个 `GetTypeId` 特化的**实现**（固定整数值）、`TypeId → ValueType` 映射表，以及 AnyValue 的拷贝/移动/Swap/取值类型等非模板成员 |
| [tests/ut/base/testcase/any_value_ut.cc](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/any_value_ut.cc) | AnyValue 的单元测试，覆盖构造、拷贝/移动语义、SameType、错误类型读取等场景 |
| [tests/ut/base/testcase/func_counter.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/func_counter.h) | 测试辅助结构体：用静态计数器记录构造/拷贝/移动/析构次数，用来验证 AnyValue 的存储策略 |
| [pkg_inc/graph/def_types.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/pkg_inc/graph/def_types.h) | 提供 `PtrToPtr` 等指针互转工具，AnyValue 内部大量使用 |

真实消费方（说明 AnyValue 是属性体系的地基）：

- 算子实现注册时保存私有属性：`base/registry/op_impl_registry.cc:420-437`（如 [`PrivateAttr` 用 `AnyValue::CreateFrom<int64_t>` 包装属性值](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/registry/op_impl_registry.cc#L420-L437)）。
- 上下文构建器向 `TilingContext` 填充属性：`base/context_builder/op_context_builder_base.cc:179-186`。
- 官方文档示例中 `NodeAttrs` 直接以 `AnyValue::CreateFrom<...>` 作为属性值（`docs/zh/api/gert_namespace/tilingdata/AppendConvertedAttrVal.md`）。

另外注意 [any_value.h:221](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/pkg_inc/graph/any_value.h#L221) 的别名 `using GeAttrValue = AnyValue;`——你在 ge 老代码里看到的「算子属性值」`GeAttrValue` 就是本讲的 `AnyValue`。

## 4. 核心概念与源码讲解

### 4.1 TypeId 体系：给每个 C++ 类型发一张「身份证」

#### 4.1.1 概念说明

类型擦除的第一步是：把「类型」本身变成一个可以比较的**运行期值**。metadef 定义：

```cpp
using TypeId = void *;
constexpr TypeId kInvalidTypeId = nullptr;
```

即 TypeId 就是一个指针大小的值（[type_id.h:33-34](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/pkg_inc/graph/type_id.h#L33-L34)）。它有两套发号方式：

1. **默认方式（地址发号）**：每个类型 `T` 对应一个静态字符 `TypeIdHolder<T>::id`，取它的地址作为 ID。
2. **特化方式（固定整数发号）**：对 22 种预定义属性类型，`GetTypeId<T>()` 直接返回常数 1~22（reinterpret 成 `void*`）。

为什么要两套？因为 metadef 是被多个 `.so` 共同包含的基础库。静态变量地址在每个 `.so` 里可能是不同的，跨 so 比较会出错；而**固定整数在任何进程、任何 so 里都相等**。所以凡是要写进属性体系、会被框架序列化识别的类型，都必须走特化、拿固定号码。

#### 4.1.2 核心流程

取一个类型的 TypeId 的流程：

```text
GetTypeId<T>()
  ├─ 去除 cv 限定与引用（remove_cv + remove_reference）得到 PureT
  ├─ T 有显式特化？（在 any_value.cc 中实现）
  │    ├─ 是 → 返回固定整数（如 GetTypeId<int64_t>() → 4）
  │    └─ 否 → 返回 &TypeIdHolder<PureT>::id（静态变量地址）
```

对应的「类型号码 → 语义化枚举」翻译流程：

```text
AnyValue::GetValueType()
  ├─ GetValueTypeId()：向 operate_ 询问当前值的 TypeId
  └─ 在 type_ids_to_value_type 映射表中查表
       ├─ 命中 → 返回 VT_STRING / VT_LIST_INT 等 ValueType 枚举
       └─ 未命中（自定义类型、double 等未登记类型）→ 返回 VT_NONE
```

#### 4.1.3 源码精读

**通用模板：地址发号。**

```cpp
template <typename T>
struct GE_FUNC_DEV_VISIBILITY GE_FUNC_HOST_VISIBILITY TypeIdHolder {
  static char_t id;
};
// ...
template <typename T>
GE_FUNC_DEV_VISIBILITY GE_FUNC_HOST_VISIBILITY TypeId GetTypeId() {
  using PureT = typename std::remove_cv<typename std::remove_reference<T>::type>::type;
  return &(TypeIdHolder<PureT>::id);
}
```

[type_id.h:25-46](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/pkg_inc/graph/type_id.h#L25-L46)：每个类型拿到一个独有的静态字符，其地址即类型 ID。`remove_cv/remove_reference` 保证 `int`、`const int`、`int&` 被归并为同一个 PureT——这就是单测里 `av1.SameType<int>()` 对左值/右值/const 值全部为 true 的原因。`GE_FUNC_DEV_VISIBILITY / GE_FUNC_HOST_VISIBILITY` 宏负责导出符号，尽量让各 so 共享同一份静态变量。

**特化声明：22 种预定义类型。** [type_id.h:48-112](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/pkg_inc/graph/type_id.h#L48-L112) 逐个声明了 `bool`、`std::string`、`float`、`int64_t`、`GeTensorDesc`、`GeTensor`、`Buffer`、`proto::GraphDef`、`NamedAttrs`、`DataType` 以及它们的 `std::vector` 组合（还有 `vector<vector<int64_t>>`、`vector<vector<float>>`）的显式特化——**只有声明，没有函数体**，函数体在 `base/any_value.cc` 里。这是 u1-l3 所讲「声明在头文件、实现编入 libmetadef.so」模式的又一次体现。

**特化实现：固定整数。**

```cpp
template <>
GE_FUNC_DEV_VISIBILITY GE_FUNC_HOST_VISIBILITY TypeId GetTypeId<bool>() {
  return reinterpret_cast<TypeId>(1);
}
// ... std::string → 2, float → 3, int64_t → 4, ... vector<DataType> → 22
```

[base/any_value.cc:17-125](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/any_value.cc#L17-L125)：22 个特化依次返回常数 1~22。注意 `kInvalidTypeId = nullptr`（即 0）保留给「空值」，`{nullptr, AnyValue::VT_NONE}` 也作为映射表的第一项（[any_value.cc:128-129](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/any_value.cc#L128-L129)）。

**号码 → 语义枚举的翻译表。**

```cpp
std::unordered_map<TypeId, AnyValue::ValueType> type_ids_to_value_type = {
    {nullptr, AnyValue::VT_NONE},
    {GetTypeId<std::string>(), AnyValue::VT_STRING},
    {GetTypeId<int64_t>(), AnyValue::VT_INT},
    // ...
};
```

[base/any_value.cc:127-153](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/any_value.cc#L127-L153)：这张表把运行期 TypeId 翻译成 `AnyValue::ValueType` 枚举。查不到就返回 `VT_NONE`——这意味着**未登记的类型（如 `double`、或你的自定义结构体）能被存取，但 `GetValueType()` 认不出它的语义类型**。这一点在 4.3 节和代码实践里都会验证。

#### 4.1.4 代码实践

1. **实践目标**：亲手验证「同类型不同值类别拿到同一个 TypeId；未登记类型拿不到语义枚举」。
2. **操作步骤**：写一小段测试（示例代码，可并入本讲的 4.3 实践一起运行）：

   ```cpp
   // 示例代码
   #include "graph/type_id.h"
   #include <cassert>
   int main() {
     int i = 1;
     const int &ci = i;
     assert(ge::GetTypeId<int>() == ge::GetTypeId<const int &>());  // cv/引用被归并
     assert(ge::GetTypeId<int>() != ge::GetTypeId<int64_t>());      // int 与 int64_t 是两个类型
     assert(reinterpret_cast<ge::TypeId>(4) == ge::GetTypeId<int64_t>());  // 固定整数发号
     assert(ge::GetTypeId<double>() != nullptr);                    // 未登记类型走地址发号
     return 0;
   }
   ```

3. **需要观察的现象**：所有断言通过，不发生崩溃。
4. **预期结果**：`GetTypeId<int64_t>()` 恒等于整数 4；`GetTypeId<double>()` 返回某个堆/BSS 地址而不是 1~22 中的任何值。
5. 断言中 `GetTypeId<int>() != GetTypeId<int64_t>()` 在 LP64 平台必然成立（int 为 4 字节、int64_t 为 8 字节，是不同类型），**其余行为可从源码直接推出，本地运行结果待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么不直接用 `sizeof(T)` 或字符串形式的类型名（`typeid(T).name()`）做 TypeId？
**答案**：`sizeof` 会碰撞——`int64_t`、`double`、指针都是 8 字节，无法区分；`typeid().name()` 的返回字符串是编译器实现相关的（gcc 与 clang、甚至不同版本之间不同），且每个 so 中 RTTI 名字比较也依赖编译器一致性，不适合做跨 so 的 ABI 契约。固定整数 + 地址双轨制在稳定性和唯一性之间取得平衡。

**练习 2**：如果上层框架想新增一个属性类型 `std::vector<double>`，需要动哪几个文件？
**答案**：至少三处——`any_value.h` 的 `ValueType` 枚举加 `VT_LIST_DOUBLE`、`type_id.h` 加 `GetTypeId<std::vector<double>>()` 的特化声明、`base/any_value.cc` 加特化实现（分配下一个整数 23）并在 `type_ids_to_value_type` 表中登记。同时由于 ValueType 枚举值会参与属性序列化，新值只能尾部追加，不能插入（与 u2-l1 中 DataType 枚举的 ABI 约束同理）。

### 4.2 AnyValue 的存储布局与操作函数表

#### 4.2.1 概念说明

打开 [any_value.h:212-219](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/pkg_inc/graph/any_value.h#L212-L219)，AnyValue 的全部数据成员只有两个：

```cpp
using ValueHolder = union {
  void *pointer;                                                  // 堆分配路径：指向 new 出来的 T
  std::aligned_storage<sizeof(void *)>::type inline_buf;          // 内联路径：就地存放小 T
};
ValueHolder holder_ = {nullptr};
void (*operate_)(OperateType ot, const AnyValue *av, void *out){nullptr};
```

64 位平台上 `sizeof(AnyValue)` 恒为 16 字节（8 字节联合体 + 8 字节函数指针），**无论装的是 int 还是嵌套 vector**。小对象（不超过一个指针大小）直接放在联合体的 `inline_buf` 里，避免一次堆分配；大对象 `new` 到堆上，联合体退化成纯指针。类型信息则完全浓缩在 `operate_` 这个函数指针里——它指向 `InlineOperations<T>::Operate` 或 `AllocateOperations<T>::Operate`，既能干活又能证明「我是什么类型存法」。`operate_ == nullptr` 即空值（`IsEmpty()`，[any_value.h:174-176](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/pkg_inc/graph/any_value.h#L174-L176)）。

#### 4.2.2 核心流程

**写入（SetValue / CreateFrom）的选路逻辑**在 `InnerSet`（[any_value.h:183-191](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/pkg_inc/graph/any_value.h#L183-L191)）：

```text
InnerSet(value)
  ├─ PureT = 去除 cv/引用后的类型
  ├─ Inline 判定：Inline = (sizeof(PureT) <= sizeof(holder_))    // 即 ≤ 8 字节
  ├─ Inline == true  → InlineOperations<PureT>::Construct(value, this)
  │                     在 holder_.inline_buf 上 placement-new 一个 T
  │                     operate_ = &InlineOperations<T>::Operate
  └─ Inline == false → AllocateOperations<PureT>::Construct(value, this)
                        holder_.pointer = new (nothrow) T(value)
                        operate_ = &AllocateOperations<T>::Operate
```

**operate_ 的五种操作**（枚举定义见 [any_value.h:194](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/pkg_inc/graph/any_value.h#L194)）：

| OperateType | 作用 | 内联版行为 | 堆分配版行为 |
| --- | --- | --- | --- |
| `kOpClear` | 析构并置空 | 对 inline_buf 调 `~T()` | `delete` 指针 |
| `kOpGetAddr` | 取数据地址 | 返回 `&inline_buf` | 返回 `holder_.pointer` |
| `kOpClone` | 拷贝到另一个 AnyValue | placement-new 拷贝 | `new T(*旧值)` 深拷贝 |
| `kOpMove` | 移动到另一个 AnyValue | 移动构造进 inline_buf | 直接接管指针，源置空 |
| `kGetTypeId` | 询问 TypeId | 返回 `GetTypeId<T>()` | 同左 |

**读取（Get\<T\>）的安全闸门**（[any_value.h:142-151](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/pkg_inc/graph/any_value.h#L142-L151)）：

```text
Get<T>()
  ├─ SameType<T>()？    // 先经 operate_ 问 kGetTypeId，与 GetTypeId<T>() 比较
  │    └─ 否 → return nullptr
  ├─ IsEmpty()？        // operate_ == nullptr
  │    └─ 是 → return nullptr
  └─ 经 kOpGetAddr 拿到地址，reinterpret_cast<const T*> 后返回
```

注意 `reinterpret_cast` 之前**一定**有 `SameType` 检查，否则把 `vector<int64_t>` 的字节流按 `int` 解释就是未定义行为。这就是「安全取值」的全部秘密。

#### 4.2.3 源码精读

**堆分配版的构造与操作表**：

```cpp
template <typename T>
void AnyValue::AllocateOperations<T>::Construct(T &&value, AnyValue *const av) {
  av->holder_.pointer = ::new (std::nothrow) T(std::forward<T>(value));
  av->operate_ = AnyValue::AllocateOperations<T>::Operate;
}
```

[any_value.h:228-232](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/pkg_inc/graph/any_value.h#L228-L232)：大对象走 `new (std::nothrow)`，分配失败不抛异常（与 u2-l2 中 AscendString 的无异常理念一致）。`Operate` 的实现（[any_value.h:233-265](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/pkg_inc/graph/any_value.h#L233-L265)）里最值得看的是 `kOpClear` 分支：

```cpp
case OperateType::kOpClear: {
  auto *const av_p = PtrToPtr<void, AnyValue>(out);
  delete PtrToPtr<void, T>(av_p->holder_.pointer);   // 用 T 的完整类型 delete —— 正确析构
  av_p->holder_.pointer = nullptr;
  av_p->operate_ = nullptr;
  break;
}
```

`delete` 一个 `void*` 是未定义行为，而这里通过模板参数 T 保证了 `delete PtrToPtr<void, T>(p)` 等价于 `delete (T*)p`，析构函数被正确调用。

**内联版的构造与操作表**（[any_value.h:266-307](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/pkg_inc/graph/any_value.h#L266-L307)）：

```cpp
template <typename T>
void AnyValue::InlineOperations<T>::Construct(const T &value, AnyValue *const av) {
  (void)::new (&(av->holder_.inline_buf)) T(value);   // placement-new 到联合体内部
  av->operate_ = AnyValue::InlineOperations<T>::Operate;
}
```

`kOpClear` 分支对 inline_buf 显式调用 `->~T()`——placement-new 出来的对象必须手动析构。`kOpMove` 分支则用移动构造把值「搬」进目标 AnyValue 的 inline_buf。

**拷贝与移动语义**（实现在 base/any_value.cc，因为它们是非模板成员）：

```cpp
AnyValue::AnyValue(AnyValue &&other) noexcept {
  if (!other.IsEmpty()) {
    other.operate_(OperateType::kOpMove, &other, this);
  }
}
AnyValue &AnyValue::operator=(const AnyValue &other) {
  if (&other == this) { return *this; }
  Clear();
  if (!other.IsEmpty()) {
    other.operate_(OperateType::kOpClone, &other, this);   // 深拷贝
  }
  return *this;
}
```

[base/any_value.cc:172-196](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/any_value.cc#L172-L196)：拷贝是**深拷贝**——单测 `CopyConstructOk`（[any_value_ut.cc:80-89](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/any_value_ut.cc#L80-L89)）正是这样验证的：`av2` 拷贝自 `av` 后，修改源字符串 `s`，`av2` 中的值仍是 `"Hello world"`。头文件里的拷贝构造（[any_value.h:85-89](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/pkg_inc/graph/any_value.h#L85-L89)）同样委托给 `kOpClone`。

**单测如何证明双存储策略**：[any_value_ut.cc:19-27](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/any_value_ut.cc#L19-L27) 定义了两个计数器结构体——`InlineFuncCounter`（一个 int32_t，≤8 字节，走内联）和 `AllocatedFuncCounter`（四个 int64_t，>8 字节，走堆分配），配合 [func_counter.h:16-114](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/func_counter.h#L16-L114) 的静态计数器精确断言拷贝/移动/析构各发生了几次。例如 `MoveConstructOk_Alloc1`（[any_value_ut.cc:119-132](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/any_value_ut.cc#L119-L132)）断言堆分配对象移动后只析构一次（指针被接管，没有多余拷贝）。

#### 4.2.4 代码实践

1. **实践目标**：用单测计数器亲眼「看见」内联与堆分配两条路径。
2. **操作步骤**：
   - 打开 `tests/ut/base/testcase/func_counter.h`，弄清 6 个静态计数器的含义。
   - 复习 `any_value_ut.cc` 中的 `CopyConstructOk_Inline1`（L56-66）与 `CopyConstructOk_Alloc1`（L68-78）。
   - 运行：`bash tests/run_test.sh -u`（构建方式见 u1-l2），或构建完成后用 `--gtest_filter='AnyValueUt.CopyConstructOk*'` 精确过滤。
3. **需要观察的现象**：两个用例断言完全相同（拷贝构造恰好 1 次、其他次数为 0），尽管一个值是 4 字节结构体、另一个是 32 字节结构体。
4. **预期结果**：两条路径对上层语义完全透明——这正是操作函数表把存储差异封装掉的效果。
5. 具体输出依赖本地构建环境，待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`AnyValue::Clear()`（[any_value.h:167-172](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/pkg_inc/graph/any_value.h#L167-L172) 为什么不能简单写成 `holder_.pointer = nullptr`？
**答案**：`Clear` 必须先析构所存对象，否则内联路径下 inline_buf 里的 T 永远不会执行析构函数、堆路径下内存泄漏。它把这件事委托给 `operate_(kOpClear, nullptr, this)`，由正确的 `InlineOperations<T>/AllocateOperations<T>::Operate` 调用 `~T()` 或 `delete`，之后才把指针和 `operate_` 置空。直接置空指针等于丢弃类型信息，再也无法安全释放。

**练习 2**：`sizeof(std::string)` 在常见 libstdc++ 实现中是 32 字节，`sizeof(std::vector<int64_t>)` 是 24 字节——它们分别走哪条存储路径？
**答案**：都大于 8 字节（`sizeof(holder_)`），都走 `AllocateOperations` 堆分配路径，联合体里存的是指针。走内联路径的典型是 `int64_t`、`float`、`bool`、指针、以及 8 字节以内的自定义结构体（如单测的 `InlineFuncCounter`）。注意判据是 `sizeof` 而不是「是否是类类型」。

**练习 3**：AnyValue 头文件里为什么用 `std::aligned_storage<sizeof(void *)>` 而不是直接 `char buf[8]`？
**答案**：`char` 数组的对齐只有 1 字节，placement-new 一个对齐要求为 8 的类型（如含 int64_t 的结构体）到它上面是未定义行为。`std::aligned_storage<sizeof(void*)>::type` 保证这块缓冲至少按指针对齐（通常 8 或 16 字节），满足所有不超过 8 字节类型的对齐要求。

### 4.3 类型判断、安全取值与 ValueType 语义枚举

#### 4.3.1 概念说明

前两个模块解决了「存」的问题，本模块解决「取的时候不出错」和「框架怎么认识这个值」：

- `SameType<T>()`：运行期类型相等判断，是 `Get/MutableGet` 的安全前置。
- `GetValue(T &value)`：返回 `graphStatus` 的稳妥取值接口——类型不符返回 `GRAPH_FAILED` 而不是崩溃。
- `GetValueType()`：把 TypeId 翻译成语义枚举 `AnyValue::ValueType`（VT_INT、VT_LIST_STRING……），供序列化、图dump、算子校验等「不关心具体 C++ 类型、只关心类别」的框架逻辑使用。

> **勘误提示**：本讲规划中的实践任务原文提到「用 `AnyValue::IsValid` 校验非法读取路径」。经核对源码，`AnyValue` 并没有 `IsValid` 这个成员（在 `pkg_inc/graph` 下全局检索也无此符号）；实际的校验信号是：`Get<T>()` 返回 `nullptr`、`GetValue` 返回 `GRAPH_FAILED`、`GetValueType()` 返回 `VT_NONE`。请以下述真实接口为准。

#### 4.3.2 核心流程

三种「类型不符」的暴露方式：

```text
av 内装的是 vector<int64_t>：
  av.Get<int64_t>()            → nullptr            （SameType 失败）
  int64_t x; av.GetValue(x)    → GRAPH_FAILED       （内部就是 Get 判空）
  av.GetValueType()            → VT_LIST_INT        （框架认识它）
  av.Get<vector<int32_t>>()    → nullptr            （元素类型不同也是不同类型！）

av 为空（operate_ == nullptr）：
  av.Get<任何类型>()            → nullptr
  av.GetValueType()            → VT_NONE

av 内装的是 double（未登记类型）：
  av.Get<double>()             → 正常取到值        （地址发号的 TypeId 照常工作）
  av.GetValueType()            → VT_NONE           （查表未命中）
```

`ValueType` 枚举的编码规律（[any_value.h:55-81](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/pkg_inc/graph/any_value.h#L55-L81)）：标量类型从 0/1 开始编号，列表类型按公式

\[ \text{VT\_LIST\_X} = \text{VT\_LIST\_BASE} + \text{VT\_X} = 1000 + \text{VT\_X} \]

即 `VT_LIST_STRING = 1000 + 1 = 1001`，`VT_LIST_INT = 1000 + 4 = 1004`。这样给定一个 ValueType 值，减去 1000 即可反查元素类型，无需第二张表。（源码注释也坦承：这组预定义类型让 AnyValue 反向依赖了 ComputeGraph 等数据结构，属于待整改的历史包袱，[any_value.h:53-54](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/pkg_inc/graph/any_value.h#L53-L54)。）

#### 4.3.3 源码精读

**SameType——一次跨「存取双方」的 TypeId 对质**：

```cpp
template <class T>
bool SameType() const noexcept {
  if (operate_ == nullptr) {
    return false;
  }
  TypeId tid = kInvalidTypeId;
  operate_(OperateType::kGetTypeId, this, &tid);   // 问值的实际类型
  return tid == GetTypeId<T>();                    // 与期望类型比较
}
```

[any_value.h:155-163](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/pkg_inc/graph/any_value.h#L155-L163)：两侧都调用同一个 `GetTypeId<T>()` 模板，预定义类型两侧都拿到固定整数，比较绝对可靠；自定义类型两侧拿的是同一编译单元可见的静态变量地址，在同一 so 内也可靠。

**GetValue——把判空包装成状态码**：

```cpp
template <typename T>
graphStatus GetValue(T &value) const {
  auto *const p = Get<T>();
  if (p == nullptr) {
    return GRAPH_FAILED;
  }
  value = *p;
  return GRAPH_SUCCESS;
}
```

[any_value.h:132-140](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/pkg_inc/graph/any_value.h#L132-L140)：单测 `GetWrongTypeFailed`（[any_value_ut.cc:457-464](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/any_value_ut.cc#L457-L464)）和 `GetEmptyOk`（L466-472）分别验证了「装了 vector<int64_t> 却按 int64_t / vector<int32_t> 取」与「空值取值」两种失败路径，断言正是 `EXPECT_NE(av.GetValue(a), GRAPH_SUCCESS)` 与 `EXPECT_EQ(av.Get<...>(), nullptr)`。

**GetValueType 的实现**：

```cpp
AnyValue::ValueType AnyValue::GetValueType() const noexcept {
  auto vt = GetValueTypeId();
  auto iter = type_ids_to_value_type.find(vt);
  if (iter == type_ids_to_value_type.end()) {
    return AnyValue::VT_NONE;
  }
  return iter->second;
}
```

[base/any_value.cc:204-211](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/any_value.cc#L204-L211)：`GetValueTypeId`（L197-203）经 `operate_(kGetTypeId)` 拿到 TypeId，再查 4.1 节那张映射表。单测 `GetTypeOk`（[any_value_ut.cc:490-499](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/any_value_ut.cc#L490-L499)）演示了正确的用法对照：`av.GetValueTypeId() == GetTypeId<std::string>()` 且 `av.GetValueType() == AnyValue::VT_STRING`。

**SetValue 的重载设计**（[any_value.h:110-130](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/pkg_inc/graph/any_value.h#L110-L130)）：万能引用版 + `const T&` 版并存（源码注释解释：只有万能引用时 `SetValue<int>(左值)` 这种显式指定模板参数的写法会推导失败），另有 `initializer_list` 版把 `{1, 2, 3}` 语法糖转成 `std::vector<T>`。每次 SetValue 先 `Clear()` 旧值再 `InnerSet`，因此一个 AnyValue 可以反复改存不同类型（见单测 `BasicTypesAssignOk`，L274-284）。

#### 4.3.4 代码实践（本讲主实践）

1. **实践目标**：写一个 demo 用例，完成「写入 int、double、vector\<string\> → 读回 → 验证失败路径 → 验证 GetValueType 对登记/未登记类型的差异」。
2. **操作步骤**：
   - 新建 `tests/ut/base/testcase/any_value_demo_unittest.cc`（依据 u1-l2：`ut_metadef` 用 glob 自动收集 `tests/ut/base/testcase/*.cc`，**无需改 CMake**）。内容如下：

   ```cpp
   // 示例代码：tests/ut/base/testcase/any_value_demo_unittest.cc
   #include <gtest/gtest.h>
   #include <string>
   #include <vector>
   #include "graph/any_value.h"

   namespace ge {
   class AnyValueDemoUt : public testing::Test {};

   TEST_F(AnyValueDemoUt, SetThreeTypesAndGetBack) {
     // 1) int：内联存储，登记类型
     AnyValue av_int = AnyValue::CreateFrom(static_cast<int64_t>(42));
     ASSERT_NE(av_int.Get<int64_t>(), nullptr);
     EXPECT_EQ(*av_int.Get<int64_t>(), 42);
     EXPECT_EQ(av_int.GetValueType(), AnyValue::VT_INT);

     // 2) double：可存可取，但未登记 → GetValueType 返回 VT_NONE
     AnyValue av_dbl = AnyValue::CreateFrom(3.14);  // 注意字面量 3.14 是 double
     ASSERT_NE(av_dbl.Get<double>(), nullptr);
     EXPECT_DOUBLE_EQ(*av_dbl.Get<double>(), 3.14);
     EXPECT_EQ(av_dbl.GetValueType(), AnyValue::VT_NONE);   // 查表未命中

     // 3) vector<string>：堆分配存储，登记类型
     std::vector<std::string> expect{"cann", "metadef", "anyvalue"};
     AnyValue av_vs = AnyValue::CreateFrom(expect);
     ASSERT_NE(av_vs.Get<std::vector<std::string>>(), nullptr);
     EXPECT_EQ(*av_vs.Get<std::vector<std::string>>(), expect);
     EXPECT_EQ(av_vs.GetValueType(), AnyValue::VT_LIST_STRING);

     // 4) 失败路径：类型不符 → Get 返回 nullptr / GetValue 返回 GRAPH_FAILED
     int64_t wrong = 0;
     EXPECT_EQ(av_vs.GetValue(wrong), GRAPH_FAILED);                       // 按 int64_t 取 vector → 失败
     EXPECT_EQ(av_vs.Get<std::vector<std::int32_t>>(), nullptr);          // 元素类型不同也是不同类型

     // 5) 失败路径：空值
     AnyValue empty;
     EXPECT_TRUE(empty.IsEmpty());
     double d = 0.0;
     EXPECT_EQ(empty.GetValue(d), GRAPH_FAILED);
     EXPECT_EQ(empty.GetValueType(), AnyValue::VT_NONE);
   }
   }  // namespace ge
   ```

   - 运行：`bash tests/run_test.sh -u`，然后 `--gtest_filter='AnyValueDemoUt.*'` 只跑本用例（gtest 过滤参数传给 `ut_metadef` 可执行文件）。
3. **需要观察的现象**：三组正常读写全部通过；`av_dbl.GetValueType()` 打印/断言为 `VT_NONE`（值为 0）而不是某个 VT_* 枚举；两条失败路径返回 `nullptr` / `GRAPH_FAILED` 且进程不崩溃。
4. **预期结果**：与上面每条断言一致。特别体会：`double` 的字面量类型陷阱——如果写 `AnyValue::CreateFrom(3.14f)` 存的是 `float`，`GetValueType()` 就会返回 `VT_FLOAT`，因为 `float` 是登记类型。这也是为什么算子属性统一使用 `int64_t`/`float`（见 any_value.h 的 `using INT/FLOAT`）而不是 `int/double`。
5. 断言结果可从源码直接推导，本地运行输出待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`av.Get<std::vector<int32_t>>()` 对一个装着 `vector<int64_t>` 的 AnyValue 返回什么？为什么？
**答案**：返回 `nullptr`。`SameType<std::vector<int32_t>>()` 中，`operate_` 报上来的 TypeId 是 `GetTypeId<std::vector<int64_t>>()`（固定整数 16），与 `GetTypeId<std::vector<int32_t>>()`（未特化，走地址发号）必然不相等，`Get` 在 reinterpret_cast 之前就返回了 nullptr。类型安全是「整个 C++ 类型」级别的，不看内存布局是否兼容。

**练习 2**：为什么 metadef 自己造 `AnyValue`，而不用标准库的 `std::any`？
**答案**：`std::any` 要求 C++17，且其内部用 `typeid`（RTTI）做类型判断——RTTI 名字跨编译器/跨库不稳定，也不适合作为序列化依据；此外 CANN 的编译基线与异常策略（`new (nothrow)`、无异常路径）与 `std::any` 的设计（`std::bad_any_cast` 抛异常）不匹配。AnyValue 自带的 TypeId 体系还能与 `ValueType` 语义枚举、属性序列化打通，这是 `std::any` 不具备的。

**练习 3**：`MutableGet<T>()`（[any_value.h:309-320](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/pkg_inc/graph/any_value.h#L309-L320) 与 `Get<T>()` 的差别是什么？什么时候用它？
**答案**：`Get` 返回 `const T*` 只读，`MutableGet` 返回 `T*` 可写，两者都先过 `SameType` 与 `IsEmpty` 闸门。需要原地修改属性值时用 `MutableGet`（如单测 `MutableGetAndModifiedOk2`，L400-408，直接对返回的 `vector<int64_t>*` 做 `push_back` 后 AnyValue 内的值同步可见，因为内联/堆指针指向的就是同一个对象）。

## 5. 综合实践

**任务：给你的「迷你算子属性表」做一个 AnyValue 驱动的读写层，并验证四种典型属性的完整存取链。**

背景：算子的属性（attr）在框架内部就是「属性名 → AnyValue」的映射。请综合运用本讲三个模块的知识：

1. 定义一个结构体（示例代码）：

   ```cpp
   // 示例代码
   #include <unordered_map>
   #include <string>
   #include "graph/any_value.h"

   struct MiniAttrHolder {
     std::unordered_map<std::string, ge::AnyValue> attrs;
     template <typename T>
     void Set(const std::string &name, const T &v) { attrs[name].SetValue(v); }
     template <typename T>
     bool Get(const std::string &name, T &v) { return attrs.count(name) && attrs[name].GetValue(v) == ge::GRAPH_SUCCESS; }
   };
   ```

2. 仿照 4.3.4 的测试文件，为 `MiniAttrHolder` 写一个 gtest 用例，覆盖：
   - 标量属性 `axis = int64_t(1)`（内联存储，`VT_INT`）；
   - 列表属性 `strides = vector<int64_t>{1,2}`（堆存储，`VT_LIST_INT`）；
   - 用 `SameType`/`GetValueType` 打印每个属性的类型，验证 `ValueType = 1000 + 元素类型` 的编码公式；
   - 故意用错误类型读取，验证返回失败而不是崩溃；
   - 用 `std::unordered_map<std::string, ge::AnyValue>` 本身作为属性表，体会 AnyValue 16 字节固定布局对容器开销的意义。
3. 运行 `bash tests/run_test.sh -u` 并用 gtest 过滤你的用例。
4. **观察重点**：属性表对四种值「无感」——这正是类型擦除让上层代码与具体属性类型解耦的收益；同时对照 `base/registry/op_impl_registry.cc:420-437`（私有属性注册）和 `base/context_builder/op_context_builder_base.cc:179-186`（上下文属性填充），你会发现这两处生产代码的写法与你的 MiniAttrHolder 骨架同构。

## 6. 本讲小结

- `AnyValue`（别名 `GeAttrValue`）是 metadef 属性体系的类型擦除容器：16 字节固定布局（8 字节联合体 + 8 字节函数指针），与所存值类型无关，因此在 ABI 边界上安全。
- 存储采用双策略：`sizeof(T) <= 8` 走 `InlineOperations`（placement-new 到联合体内），否则走 `AllocateOperations`（`new (nothrow)` 堆分配）；拷贝是深拷贝，移动直接接管。
- 所有运行期操作（Clear/GetAddr/Clone/Move/GetTypeId）统一收敛到一个函数指针 `operate_` 分发——这是手写类型擦除的核心手法，也是 `operate_ == nullptr` 能代表「空值」的原因。
- `TypeId` 双轨发号：22 种预定义属性类型由 `base/any_value.cc` 的特化分配固定整数 1~22（跨 so 稳定），其余类型用 `TypeIdHolder<T>::id` 的地址（仅同 so 内可靠）。
- 安全取值三件套：`SameType<T>()` 判断类型、`Get<T>()` 失败返回 `nullptr`、`GetValue` 失败返回 `GRAPH_FAILED`；框架侧语义识别用 `GetValueType()`（TypeId → ValueType 查表，未登记类型返回 `VT_NONE`）。
- `ValueType` 枚举按 `列表类型 = 1000 + 标量类型` 编码，是属性序列化的契约，只能尾部追加。

## 7. 下一步学习建议

下一讲（u2-l4）将进入 `gert::Shape/Stride/Tensor` 张量描述体系。你会在那里再次见到 AnyValue 的身影——`GeTensorDesc`、`NamedAttrs` 都是 AnyValue 预定义列表中的类型，属性值可以直接装一个张量描述。建议顺带阅读：

- 用 `grep -rn "NamedAttrs" inc pkg_inc` 观察一个有趣事实：`NamedAttrs` 在本仓只有前向声明（[type_id.h:20](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/pkg_inc/graph/type_id.h#L20) 等），完整定义不在此仓——TypeId/AnyValue 体系正是靠这种「不完整类型也能发号」的能力，把对上层数据结构的依赖降到最低。
- [base/any_value.cc](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/any_value.cc) 的 `Swap` 实现（L155-170），体会如何用三次 `kOpMove` 实现异常安全的交换。
- 单元一（u1-l3）的「声明 → 桥接 → 实现」三层结构，对照本讲 `type_id.h` 声明 / `any_value.cc` 实现的组合，这一模式将在整个学习手册中反复出现。
