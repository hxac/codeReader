# 16 字节值表示：njs_value_t

## 1. 本讲目标

上一讲 u2-l1 我们看清了 `njs_vm_t` 的「外壳」——创建、编译、执行、克隆、销毁。但那些 API 在参数和返回值里反复出现一个类型：`njs_value_t`。本讲就把这 16 字节拆开，讲清 njs 引擎里**一切 JS 值**在内存里到底长什么样。学完后你应该能够：

- 说清为什么 `njs_value_t` 被设计成**固定 16 字节**，以及这 16 字节在 `data` / `string` / 匿名三种「视图」下如何重叠排布。
- 根据 `type` 字段判断一个值属于原始值（null/undefined/boolean/number/symbol/string）还是对象类（object/array/function/…），并指出它的 **payload**（真正承载数据的 8 字节）当前是 `double`、指针还是 `prop_handler`。
- 看懂「atom_id 与数值的双关编码」：前 4 字节既能当字符串的 atom_id、又能当 symbol 的 id；以及用最高位（`0x80000000`）把「整数键」直接编码进 atom_id 而无需驻留成字符串的技巧。
- 掌握日常读写值的工具集：`njs_argument`、`njs_value_assign`（为什么是 `memcpy`）、`njs_value_atom`、`njs_number(value)`、`njs_object(value)`，以及一整套 `njs_is_*` 类型测试宏和预定义的 `njs_value_null` / `njs_value_undefined` 等常量。

本讲是后续 u2-l3（内存池/哈希表）、u2-l4（Atom 表）、u3（编译前端）、u4（字节码执行）的共同地基——因为每一条字节码指令、每一次属性查找、每一次函数调用，搬动的都是 `njs_value_t`。

---

## 2. 前置知识

### 2.1 为什么 JS 引擎需要一种「统一的值表示」

JavaScript 是动态类型语言：同一个变量，上一行可以是数字 `42`，下一行可以是字符串 `"hi"`，再下一行可以是一个对象 `{}`。但底层是 C，C 的每个类型都有固定大小。引擎必须用**一种统一的 C 类型**来表示「任意一种 JS 值」，让所有变量、数组元素、函数参数、对象属性值都用同一种容器存放。

njs 选用的这个统一容器就是 `njs_value_t`。你会看到数组 `njs_array_t` 的元素区是 `njs_value_t *start`（一排紧挨着的值），函数参数也是一个 `njs_value_t` 数组。所以「值表示」的设计直接决定了引擎**其它所有数据结构**的形态。

### 2.2 标签联合体（tagged union）：最直觉的方案

表示「多种可能形态之一」的 C 惯用法是 **带标签的联合体（tagged union）**：

- 一个 `type` 字段说明「当前是哪种形态」（标签）；
- 一个 `union` 把多种形态的 payload 叠放在同一段内存里（联合）；
- 读写时先看 `type`，再用对应的方式解释 payload。

njs 采用的正是这个最朴素、最易读的方案。与之相对，有些引擎（如 V8 的指针压缩、SpiderMonkey 的 NaN-boxing）会用更紧凑但更绕的位编码把值塞进 8 字节甚至一个指针里。njs 为了**简单与可读**，选择「朴素 tagged union + 16 字节」的方案，代价是每个值比 NaN-boxing 多占内存，换来的是源码里几乎不需要 pack/unpack 操作。这是 njs 作为「教学价值很高的引擎」的一个典型取舍。

### 2.3 复习：十六进制与位运算

本讲会反复出现十六进制位运算，先复习几个要点：

- 一个 `uint32_t` 共 32 位，写成十六进制是 8 位，例如 `0x80000000`。
- `0x80000000` 的最高位（第 31 位）是 1，其余为 0。
- `x & 0x80000000`：只保留最高位，用来「测试最高位是否为 1」。
- `x & 0x7FFFFFFF`：清掉最高位，保留低 31 位。
- `x | 0x80000000`：把最高位置 1。

这三条正是后面「整数键编码」的全部数学基础，记住即可。

### 2.4 承接 u2-l1

u2-l1 讲到 `njs_vm_clone` 时提到克隆会重建「atom 表」「全局 levels」，讲到 `njs_vm_destroy` 时说「所有运行时分配都来自 `mem_pool`」。这些机制搬运的「内容」绝大多数就是 `njs_value_t`。本讲把这些值本身的内存布局讲透，下一讲 u2-l3 再讲存放它们的池与哈希表。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/njs_value.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h) | 本讲主角。定义 `njs_value_type_t` 枚举、`union njs_value_s`（值的 16 字节布局）、所有 `njs_is_*` 测试宏、`njs_number/object/function` 等访问宏、`njs_set_*` 写入内联函数、`njs_value` 等字面量初始化宏。 |
| [src/njs_value.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.c) | 值相关的「重」函数实现：预定义常量（`njs_value_null` 等）、`njs_type_string`、属性查找 `njs_property_query` / `njs_value_property`。本讲主要用其中的预定义常量与 `njs_value_property` 的「整数键快路径」来佐证双关编码。 |
| [src/njs.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs.h) | 公共 API 头。定义 `njs_opaque_value_t`（外部可见的不透明存储）、`njs_argument`、`njs_value_assign`、`njs_value_atom`、`njs_atom_is_number` / `njs_atom_number` / `njs_number_atom` 三个原子位运算宏。 |
| [src/njs_string.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_string.h) | 字符串的底层结构 `njs_string_s`（`start` 指针 + `length` + `size`），字符串值 payload 指向它。 |
| [src/njs_number.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_number.h) | `njs_uint32_to_string` 等数字↔字符串转换，本讲引用它佐证「小整数被编码成 number atom」。 |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**4.1 16 字节标签联合体**（值的整体布局）、**4.2 类型枚举与 payload**（type 标签 + 8 字节 payload 如何随类型变化 + atom 双关）、**4.3 值访问宏**（日常读写值的工具集）。

### 4.1 16 字节标签联合体

#### 4.1.1 概念说明

`njs_value_t` 是 njs 里**唯一**的 JS 值类型。它的核心设计是：

1. **固定 16 字节**。无论这个值是数字、字符串还是对象，占用的内存都恰好是 16 字节。
2. **三种重叠视图**。同一个 16 字节块，可以用 `data` 视角（给 number/object/function 用）、`string` 视角（给 string 用）或匿名视角（只看头部）来解释，三种视角在内存上**互相重叠**。
3. **头部共享、尾部是 payload**。前 8 字节是「头部」（类型标签 + truth + atom/magic），后 8 字节是「payload」（真正承载数据的 `double` 或指针）。

为什么固定 16 字节这么重要？因为它让值可以**紧凑地排成一排**，并用最简单的指针算术定位第 n 个：

- 数组的元素区就是一排连续的 16 字节，`arr[i]` 是 `&start[i]`；
- 函数的第 n 个参数是「参数数组起点 + n×16 字节」——这正是 `njs_argument` 宏做的事（见 4.3）。

固定大小换来的是「值到处都能用指针算术搬运」，这解释了为什么 `njs_argument` 敢写死「乘以 16」。

#### 4.1.2 核心流程：16 字节的布局图

把 16 字节拆成 4 个 `uint32_t`（记作 `filler[0..3]`）来看，三种视图的叠加关系如下：

| 字节偏移 | `data` 视图字段 | `string` 视图字段 | 匿名视图字段 | filler |
|---|---|---|---|---|
| 0–3   | `magic32`   | `atom_id`   | `atom_id` | `filler[0]` |
| 4     | `type`（8 位） | `type`（8 位） | `type`（8 位） | `filler[1]` 低字节 |
| 5     | `truth`     | `truth`     | `truth`   | `filler[1]` |
| 6–7   | `magic16`   | `token_type`, `token_id` | （未用） | `filler[1]` |
| 8–15  | `u`（`double` 或各类指针 / `prop_handler`） | `data`（`njs_string_t *`） | （未用） | `filler[2]`, `filler[3]` |

关键观察：

- **前 4 字节是重叠的**：`data.magic32` 与 `string.atom_id` 占的是**同一段内存**。所以读取 `filler[0]`，对 string 值就等于读 `atom_id`，对 symbol 值就等于读 `magic32`（symbol 的 id）。这是「双关编码」的物理基础。
- **第 4、5 字节也是重叠的**：两种视图都把这里放成 `type` + `truth`，所以无论怎么解释，类型标签和真值位都在同一处。
- **后 8 字节是 payload**：对 `data` 视图是一个 `u` 联合（`double` 或各种指针），对 `string` 视图是一个指向 `njs_string_t` 的指针。

为什么是 16 而不是更小？因为 payload 必须能装下一个 `double`（8 字节），头部又至少需要 `type`（1）+ `truth`（1）+ `atom_id/magic32`（4）+ `magic16`（2）= 8 字节。8 + 8 = 16，恰好是 2 的幂，对齐友好、缓存友好。

#### 4.1.3 源码精读

值的定义是本讲最核心的一处，整个 `union njs_value_s`：

```c
union njs_value_s {
    struct {
        uint32_t                      magic32;
        njs_value_type_t              type:8;  /* 5 bits */
        uint8_t                       truth;
        uint16_t                      magic16;
        union {
            double                    number;
            njs_object_t              *object;
            njs_array_t               *array;
            ...
            njs_function_t            *function;
            njs_function_lambda_t     *lambda;
            ...
            njs_prop_handler_t        prop_handler;
            njs_value_t               *value;
            void                      *data;
        } u;
    } data;

    struct {
        uint32_t                      atom_id;
        njs_value_type_t              type:8;  /* 5 bits */
        uint8_t                       truth;
        uint8_t                       token_type;
        uint8_t                       token_id;
        njs_string_t                  *data;
    } string;

    struct {
        uint32_t                      atom_id;
        njs_value_type_t              type:8;  /* 5 bits */
        uint8_t                       truth;
    };
};
```

见 [src/njs_value.h:93-140](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L93-L140)。三个 `struct` 叠在同一个 union 里，对应 4.1.2 表里的三种视图。注意 `type:8` 是位域，注释 `/* 5 bits */` 说明作者心里清楚类型枚举的最大值只要 5 位就能装下（最大 `NJS_DATA_VIEW = 25 = 0x19`，确实 5 位够），但实际分配了 8 位（一个字节），留了余量。

`truth` 字段有一段很重要的注释，解释它存在的意义：

```c
/*
 * The truth field is set during value assignment and then can be
 * quickly tested by logical and conditional operations regardless
 * of value type.
 */
uint8_t                       truth;
```

见 [src/njs_value.h:99-103](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L99-L103)。意思是：**一个值的「布尔真值」在它被赋值的那一刻就预先算好、存进 `truth`**。之后 `if (x)`、`a && b` 这类逻辑判断不用再去判断 `x` 到底是数字还是字符串、再调 ToBoolean，直接读 `truth` 一个字节即可。对数字，真值规则是「非零且非 NaN」：

```c
#define njs_is_number_true(num)                                               \
    (!isnan(num) && num != 0)
```

见 [src/njs_value.h:491-492](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L491-L492)。后面 4.2 会看到 `njs_set_number` 在写入时就用它填好了 `truth`。

公共头里还有一个与值大小强相关的「外部存储类型」：

```c
/*
 * njs_opaque_value_t is the external storage type for native njs_value_t type.
 * sizeof(njs_opaque_value_t) == sizeof(njs_value_t).
 */
typedef struct {
    uint32_t                        filler[4];
} njs_opaque_value_t;

/* sizeof(njs_value_t) is 16 bytes. */
```

见 [src/njs.h:42-53](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs.h#L42-L53)。`njs_opaque_value_t` 是**对外暴露的不透明容器**：外部代码不需要、也不应该看到内部那个复杂的 union，只需要知道「这是一个 4×4=16 字节的盒子」。而 `filler[0]` 正是 4.1.2 表里的前 4 字节——后面 `njs_value_atom` 宏就读它。

#### 4.1.4 代码实践

**实践目标**：亲手验证「三种视图重叠」与「16 字节固定大小」。

**操作步骤**：

1. 打开 [src/njs_value.h:93-140](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L93-L140)，对照 4.1.2 的布局表，逐字段标注每个字段落在第几字节。
2. 确认 `data.magic32`（偏移 0–3）与 `string.atom_id`（偏移 0–3）重叠：它们都在 union 的最开头。
3. 确认 payload 位置：`data.u`（偏移 8–15）与 `string.data`（偏移 8–15，一个 `njs_string_t *` 指针）重叠。

**需要观察的现象**：

- `data` 视图和 `string` 视图的**头部（前 8 字节）字段含义不同但位置一致**：`magic32`↔`atom_id`、`magic16`↔`token_type/token_id`。
- 两个视图的 `type` 和 `truth` 都在第 5、6 字节（`filler[1]`）的同一位置，所以无论用哪种视图读 `type`/`truth` 结果一致。

**预期结果**：你能口头复述「同一个 16 字节，对 number 我看 `data.u.number`，对 string 我看 `string.data` 指针，对 symbol 我看 `data.magic32`」。

> 待本地验证（可选）：若你已按 u1-l3 构建 `build/njs`，可写一个小 C 片段（在 `src/` 之外、作为外部程序链接 `libnjs.a`）打印 `sizeof(njs_value_t)` 与 `sizeof(njs_opaque_value_t)`，应都得 16。若不方便编译，直接阅读 [src/njs.h:51](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs.h#L51) 的注释 `/* sizeof(njs_value_t) is 16 bytes. */` 即可确认。

#### 4.1.5 小练习与答案

**练习 1**：为什么 payload（后 8 字节）必须能装下 `double`，而不能更小？

**参考答案**：因为 JS 的 `number` 类型就是 IEEE 754 双精度浮点，占 8 字节。如果 payload 小于 8，就无法内联存放一个数字，每次用数字都得额外分配+指针解引用，性能和内存都不可接受。所以 payload 定为 8 字节是「为了能直接内联 number」的硬约束。

**练习 2**：三种视图里，哪一种字段最少？它存在的意义是什么？

**参考答案**：匿名视图（[njs_value.h:135-139](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L135-L139)）只有 `atom_id`、`type`、`truth` 三个字段。它的意义是提供一个「只关心头部、不关心 payload」的统一访问入口——任何类型的值都能安全地读 `value->type`、`value->truth`、`value->atom_id`，而不必先知道它是 `data` 视图还是 `string` 视图。代码里大量 `value->type` 判断就是走这个匿名视图。

---

### 4.2 类型枚举与 payload

#### 4.2.1 概念说明

光有 16 字节容器还不够，还需要一套「类型标签」告诉引擎当前这 16 字节该怎么解释。njs 用枚举 `njs_value_type_t` 当标签，并刻意**把枚举值排成有规律的顺序**，使得「类型判断」可以用一个 `<=` / `>=` 比较完成，而不必逐个 `==`。

每种类型对应一种 payload 的解释方式：

| 类型 | 枚举值 | payload（`u` 联合）含义 |
|---|---|---|
| `NJS_NULL` / `NJS_UNDEFINED` | 0 / 1 | payload 不重要（真值固定为 false / undefined 的 NaN） |
| `NJS_BOOLEAN` | 2 | payload 不重要，真假由 `truth` 决定（常量直接复用 `njs_value_true/false`） |
| `NJS_NUMBER` | 3 | `u.number`：一个 `double` |
| `NJS_SYMBOL` | 4 | `u.value`：指向描述字符串；`magic32`：symbol 的 id |
| `NJS_STRING` | 5 | `string.data`：指向 `njs_string_t`；`atom_id`：字符串的 atom（见下） |
| `NJS_DATA` | 6 | `u.data`：任意指针；`magic32`：tag（用于外部数据的类型标记） |
| `NJS_INVALID` | 7 | 哨兵值：未初始化的数组槽、未声明变量、原生属性 getter 占位 |
| `NJS_OBJECT` 起 | ≥ 0x10 | `u.object`（或 array/function/…）：指向堆上的对象结构体 |

这里有两类值需要特别理解：

- **原始值（primitive）**：null、undefined、boolean、number、symbol、string。它们的 payload 要么内联（number 的 `double`），要么指向一个不可变的底层结构（string 的 `njs_string_t`）。判断原始值只需 `type <= NJS_STRING`。
- **对象类**：从 `NJS_OBJECT`(0x10) 起的所有类型，payload 都是**指向堆上某个对象结构体的指针**。判断对象只需 `type >= NJS_OBJECT`。

此外还有一个贯穿全引擎的「双关」技巧：前 4 字节（`atom_id` = `magic32`）既能表示字符串/symbol 的 atom，也能把一个**整数**直接编码进去当属性键用。这是本模块的重点之一。

#### 4.2.2 核心流程：类型分层、payload 选择与 atom 双关

**A. 类型分层与「范围判断」宏**

作者把原始值排在 `0..NJS_STRING`，把对象类排在 `0x10` 起，于是一批常用判断都能写成范围比较：

```
njs_is_null_or_undefined(v)            :  type <= NJS_UNDEFINED   (即 0,1)
njs_is_null_or_undefined_or_boolean(v):  type <= NJS_BOOLEAN      (即 0,1,2)
njs_is_numeric(v)                      :  type <= NJS_NUMBER       (即 0,1,2,3)
njs_is_primitive(v)                    :  type <= NJS_STRING        (即 0..5)
njs_is_object(v)                       :  type >= NJS_OBJECT        (即 >=0x10)
```

这里 `njs_is_numeric` 把 null/undefined/boolean 也算作「可参与数学运算」（注释里说 true→1、null/false→0、undefined→NaN），正是依赖枚举顺序。这种「用顺序换简洁」的设计在源码里随处可见，是读 njs 代码的基本功。

**B. payload 随类型选择**

一个值被创建时，根据它的类型，引擎往 payload 里塞不同的东西：

- number：写 `u.number`（一个 double），并把 `atom_id` 清 0；
- symbol：写 `u.value`（描述串指针），把 id 写进 `magic32`；
- object/array/function/…：写 `u.object`/`u.array`/`u.function`（堆指针）；
- 外部数据：写 `u.data`（指针），把 tag 写进 `magic32`。

**C. atom_id 与数值的双关编码**

属性查找的接口是 `njs_value_property(vm, value, atom_id, retval)`——键用 `atom_id`（一个 `uint32_t`）传递，而不是用一个 `njs_value_t`。通常 `atom_id` 是一个**字符串**在 atom 表里的下标（atom 表详见 u2-l4）。但对数组下标这种「整数键」，njs 不想把 `"0"`、`"1"`、`"2"`… 全部驻留成字符串，于是用最高位做标记，把整数直接塞进 atom_id：

\[ \texttt{atom\_id} = n\ \ |\ \ \texttt{0x80000000} \]

即「最高位置 1 表示这是个整数键，低 31 位就是整数本身」。配套的三个位运算宏：

```c
#define njs_atom_is_number(atom_id) ((atom_id) & 0x80000000)
#define njs_atom_number(atom_id)    ((atom_id) & 0x7FFFFFFF)
#define njs_number_atom(n)          ((n) | 0x80000000)
```

见 [src/njs.h:68-70](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs.h#L68-L70)。于是 `njs_atom_is_number` 一眼就能区分「整数键」和「字符串 atom」，`njs_value_property` 在进入属性查找前先判一下，是整数键就走数组的超快路径。

> 关键区分（容易混淆）：一个 **number 值**（`NJS_NUMBER`）把它的 double 存在 `u.number`，且 `atom_id == 0`（最高位**没有**置位，它不是 number atom，只是一个普通数字值）。而一个 **number atom**（当属性键用）是 `njs_number_atom(n)`，它**不是**某个值的 `u.number`，只是一个 32 位键。两者不要搞混：前者是「值」，后者是「键」。

#### 4.2.3 源码精读

先看类型枚举本身，注意注释里处处强调「顺序被某某宏使用」：

```c
typedef enum {
    NJS_NULL,
    NJS_UNDEFINED,
    /* The order of the above type is used in njs_is_null_or_undefined(). */
    NJS_BOOLEAN,
    /* ...used in njs_is_null_or_undefined_or_boolean(). */
    NJS_NUMBER,
    /* The order of the above type is used in njs_is_numeric(). ... */
    NJS_SYMBOL,
    NJS_STRING,
    /* The order of the above type is used in njs_is_primitive(). */
    NJS_DATA,
    /* The invalid value type is used: for uninitialized array members, ... */
    NJS_INVALID,

    NJS_OBJECT                = 0x10,
    NJS_ARRAY,
    NJS_FUNCTION,
    NJS_REGEXP,
    NJS_DATE,
    NJS_TYPED_ARRAY,
    NJS_PROMISE,
    NJS_OBJECT_VALUE,
    NJS_ARRAY_BUFFER,
    NJS_DATA_VIEW,
    NJS_VALUE_TYPE_MAX
} njs_value_type_t;
```

见 [src/njs_value.h:16-64](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L16-L64)。原始值在 `0..NJS_DATA(6)`（加上 `NJS_INVALID(7)` 这个哨兵），对象类统一从 `0x10` 起——这条 `0x10` 分水岭正是 `njs_is_object` 用 `type >= NJS_OBJECT` 判断的依据。

配套的范围判断宏，集中体现了「顺序即语义」：

```c
#define njs_is_null_or_undefined(value)   ((value)->type <= NJS_UNDEFINED)
#define njs_is_numeric(value)             ((value)->type <= NJS_NUMBER)
#define njs_is_primitive(value)           ((value)->type <= NJS_STRING)
#define njs_is_object(value)              ((value)->type >= NJS_OBJECT)
```

见 [src/njs_value.h:469-470](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L469-L470)、[495-496](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L495-L496)、[542-543](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L542-L543)、[556-557](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L556-L557)。

再看「写入时如何选 payload」。`njs_set_number` 写一个数字值：

```c
njs_inline void
njs_set_number(njs_value_t *value, double num)
{
    value->data.u.number = num;
    value->type = NJS_NUMBER;
    value->data.truth = njs_is_number_true(num);
    value->atom_id = 0 /* NJS_ATOM_STRING_unknown */;
}
```

见 [src/njs_value.h:787-794](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L787-L794)。注意三个细节：(1) double 写进 `data.u.number`；(2) `truth` 用 `njs_is_number_true` 预算好；(3) **`atom_id` 被清成 0**——印证了 4.2.2 里「number 值的 atom_id 是 0、不是 number atom」这一点。`njs_set_int32` / `njs_set_uint32`（[njs_value.h:801-818](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L801-L818)）同理。

写一个 symbol，把 id 放进 `magic32`（= 前 4 字节 = `atom_id`），把描述串放进 `u.value`：

```c
njs_inline void
njs_set_symbol(njs_value_t *value, uint32_t symbol, njs_value_t *name)
{
    value->data.magic32 = symbol;
    value->type = NJS_SYMBOL;
    value->data.truth = 1;
    value->data.u.value = name;
}
```

见 [src/njs_value.h:821-828](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L821-L828)。对应地，取 symbol 的 id 就是读 `magic32`：

```c
#define njs_symbol_key(value)   ((value)->data.magic32)
```

见 [src/njs_value.h:759-760](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L759-L760)。这里能直观看到「`magic32` 与 `atom_id` 是同 4 字节」的设计收益：symbol 的 id 既可以当 magic 用、也能像 atom 一样被 `njs_value_atom` 读出来。

对象类一律写一个堆指针。以普通对象和函数为例：

```c
njs_inline void
njs_set_object(njs_value_t *value, njs_object_t *object)
{
    value->data.u.object = object;
    value->type = NJS_OBJECT;
    value->data.truth = 1;
}

njs_inline void
njs_set_function(njs_value_t *value, njs_function_t *function)
{
    value->data.u.function = function;
    value->type = NJS_FUNCTION;
    value->data.truth = 1;
}
```

见 [src/njs_value.h:841-847](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L841-L847) 与 [src/njs_value.h:896-902](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L896-L902)。注意**所有对象类的 `truth` 恒为 1**——因为任何对象在 JS 里都是 truthy。payload 指向的 `njs_object_t` 结构（双哈希 + `__proto__`）会在 u5-l1 详细讲，这里只需知道「对象值 = 一个指向堆对象的指针」。

外部/数据类（`NJS_DATA`）用 `magic32` 当 tag、`u.data` 当指针：

```c
njs_inline void
njs_set_data(njs_value_t *value, void *data, njs_data_tag_t tag)
{
    value->data.magic32 = tag;
    value->data.u.data = data;
    value->type = NJS_DATA;
    value->data.truth = 1;
}
```

见 [src/njs_value.h:831-838](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L831-L838)。配合 `njs_make_tag` 把一个「原型 id」编码成 tag：

```c
#define njs_make_tag(proto_id)  (((njs_uint_t) proto_id << 8) | NJS_DATA_TAG_EXTERNAL)
```

见 [src/njs_value.h:546-547](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L546-L547)。这就是 njs 给「宿主/外部对象」打类型标签的方式——把外部原型编号挪到 tag 的高字节，低字节用 `NJS_DATA_TAG_EXTERNAL` 做标志位。`njs_is_data`（[njs_value.h:550-553](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L550-L553)）据此判断「这是不是某种外部数据」。完整的「外部对象」机制（NGINX 里 `r`/`s` 对象就建立其上）会在 u5-l4 专讲。

最后看「number atom 双关」的两个铁证。

**铁证 1：属性查找的整数快路径。** `njs_value_property` 一进来就先判 `njs_atom_is_number`：

```c
njs_int_t
njs_value_property(njs_vm_t *vm, njs_value_t *value, uint32_t atom_id,
    njs_value_t *retval)
{
    ...
    if (njs_fast_path(njs_atom_is_number(atom_id))) {
        index = njs_atom_number(atom_id);
        ...  /* 走 typed array / fast array 的下标快路径 */
    }
    ...
}
```

见 [src/njs_value.c:1059-1097](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.c#L1059-L1097)。如果 `atom_id` 最高位是 1，它就是个整数键，直接用 `njs_atom_number` 取出下标走快路径，根本不碰 atom 哈希表。这就是「整数键不需要驻留成字符串」带来的性能收益。

**铁证 2：小整数字符串的惰性物化。** `njs_uint32_to_string` 把一个 `uint32` 转成它的字符串表示时，对小于 \( 2^{31} \) 的数**不分配任何字节**，只往 `atom_id` 塞一个 number atom、把 `string.data` 置空：

```c
njs_inline njs_int_t
njs_uint32_to_string(njs_vm_t *vm, njs_value_t *value, uint32_t u32)
{
    ...
    if (!(u32 & 0x80000000)) {
        value->type = NJS_STRING;
        value->data.truth = (u32 != 0);
        value->atom_id = njs_number_atom(u32);
        value->string.data = NULL;
        return NJS_OK;
    }
    ...
}
```

见 [src/njs_number.h:181-193](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_number.h#L181-L193)。也就是说，字符串 `"123"` 在 njs 内部可能根本没有任何字节拷贝，只存了一个 number atom `123 | 0x80000000`。等真正需要它的字节（比如要打印、要拼接）时，`njs_string_get` 发现 `string.data == NULL` 但 `atom_id != 0`，再调 `njs_atom_to_value` 把字符串**惰性物化**出来：

```c
#define njs_string_get(vm, value, str)                                        \
    do {                                                                      \
        njs_value_t  _dst;                                                    \
        njs_assert(njs_is_string(value));                                     \
        if (njs_slow_path((value)->string.data == NULL)) {                    \
            njs_assert((value)->atom_id != 0 /* NJS_ATOM_STRING_unknown */);  \
            njs_atom_to_value(vm, &_dst, (value)->atom_id);                   \
            ...                                                               \
        } else { ... }                                                        \
    } while (0)
```

见 [src/njs_value.h:524-539](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L524-L539)。这套「number atom + 惰性物化」是 njs 在「值表示」层面节省内存与时间的精髓，理解了它你就理解了为什么遍历大数组的下标几乎不产生字符串分配。

补充：字符串值的真正字节存放在 `njs_string_t` 里：

```c
struct njs_string_s {
    u_char    *start;
    uint32_t  length;   /* Length in UTF-8 characters. */
    uint32_t  size;
};
```

见 [src/njs_string.h:73-77](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_string.h#L73-L77)。一个字符串值 = `string` 视图的 `atom_id`（可能是 0 表示「非驻留串」）+ `string.data` 指向这个 `{start, length, size}` 结构。`length` 是 UTF-8 字符数、`size` 是字节数，二者分离是为了支持多字节字符（纯 ASCII 时 `length == size`）。

#### 4.2.4 代码实践

**实践目标**：亲手写出 number 值与 string 值各自每个字段的含义，并定位三大类型常量。

**操作步骤**：

1. 在 [src/njs_value.h:16-64](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L16-L64) 定位 `NJS_NUMBER`、`NJS_STRING`、`NJS_OBJECT` 的定义，记录它们的枚举值（3、5、0x10）。
2. 对一个 number 值（由 `njs_set_number` 写入），写出 16 字节里每个字段的含义：
   - 字节 0–3（`atom_id`/`magic32`）：`0`（见 `njs_set_number` 的 `value->atom_id = 0`）；
   - 字节 4（`type`）：`NJS_NUMBER`(3)；
   - 字节 5（`truth`）：`!isnan(num) && num != 0`；
   - 字节 6–7（`magic16`）：未用；
   - 字节 8–15（`u.number`）：那个 double 本身。
3. 对一个 string 值（由 `njs_uint32_to_string` 写入小整数，或由 `njs_string_new` 写入普通串），写出字段含义：
   - 字节 0–3（`atom_id`）：普通串是它在 atom 表的下标（或 0 表示非驻留）；小整数字符串是 `njs_number_atom(u32)`；
   - 字节 4（`type`）：`NJS_STRING`(5)；
   - 字节 5（`truth`）：长度是否非 0；
   - 字节 6–7（`token_type`/`token_id`）：未用；
   - 字节 8–15（`data`）：指向 `njs_string_t`；小整数字符串时为 `NULL`（惰性）。

**需要观察的现象**：

- number 把「真正的数据」放在 **payload（后 8 字节）**，前 4 字节是 0；
- string 把「真正的数据」放在 **payload 是一个指针**，前 4 字节是 atom_id（小整数情况下编码了整数本身）。

**预期结果**：你能用一句话概括「number 值靠 payload 存 double，string 值靠 payload 存指针、靠前 4 字节存 atom」。

> 待本地验证（可选）：构建 `build/njs` 后，执行 `./build/njs -d -c 'var a = [10, 20, 30]; a[1]'` 反汇编，对照下一讲 u3-l5 的字节码格式，能看到对 `a[1]` 的访问用的是整数键（不会出现字符串 `"1"` 的 atom 化操作），印证 4.2.2 的整数快路径。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `njs_is_numeric` 用 `type <= NJS_NUMBER` 就能覆盖 null/undefined/boolean/number 四种？如果有人把 `NJS_BOOLEAN` 的枚举值改到 `NJS_STRING` 之后，会出什么问题？

**参考答案**：因为枚举顺序被刻意排成 null(0) < undefined(1) < boolean(2) < number(3)，`<= NJS_NUMBER` 自然包含前四种。如果把 boolean 挪到 string 之后，`type <= NJS_NUMBER` 就不再包含 boolean，于是 `true + 1` 这类「布尔参与数学运算」的场景会被错误地当成非数值处理，破坏 `njs_is_numeric` 的语义。这就是为什么 [njs_value.h:16-64](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L16-L64) 里那些注释反复强调「顺序被某某宏使用」——枚举顺序是一种隐式契约，不能随便调。

**练习 2**：`njs_uint32_to_string` 对 `u32 & 0x80000000` 为真（即 ≥ \( 2^{31} \)）的大数走的是另一条分支（`njs_string_alloc`），为什么小整数能省、大整数不能省？

**参考答案**：因为 number atom 的编码 `n | 0x80000000` 用最高位当「这是整数键」的标志位，只有低 31 位能存数值，所以只能表示 \([0, 2^{31}-1]\) 范围的整数。`u32` 最高位为 1 时无法再用 number atom 编码（会和标志位冲突），只能老老实实分配字符串字节。这是用 1 位换来的范围限制，是「双关编码」的代价。

**练习 3**：一个 string 值的 `string.data == NULL` 但 `atom_id != 0`，这说明什么？什么时候它的字节才会真正被分配？

**参考答案**：说明这是一个「只驻留了 atom、还没物化字节」的惰性字符串（典型如 `njs_uint32_to_string` 产生的小整数字符串，或某些 atom 表里的已知串）。它的字节在**第一次被真正需要**时（如 `njs_string_get` 要读字节去打印/拼接）才由 `njs_atom_to_value` 物化出来。这是一种「按需付钱」的内存优化。

---

### 4.3 值访问宏

#### 4.3.1 概念说明

4.1 讲了值的**布局**，4.2 讲了值的**类型与 payload**，本模块讲日常读写值用的**工具宏**。njs 几乎从不用 `value->data.u.number` 这种全限定写法，而是包了一层宏/内联函数，让代码更短、更安全、意图更明确。这套工具分四类：

1. **定位宏**：在值数组里找第 n 个值——`njs_argument`、`njs_arg`、`njs_lvalue_arg`。
2. **拷贝宏**：把一个值复制到另一个位置——`njs_value_assign`（为什么是 `memcpy`）。
3. **读取宏**：按类型取出 payload——`njs_number(value)`、`njs_object(value)`、`njs_function(value)`、`njs_bool(value)`、`njs_value_atom(value)`。
4. **类型测试宏 + 预定义常量**：`njs_is_*` 系列，以及 `njs_value_null` / `njs_value_undefined` / `njs_value_true` 等只读常量。

掌握这四类，你就能读懂 njs 内核里 90% 涉及值的代码。

#### 4.3.2 核心流程：定位 → 拷贝 → 读取 → 判断

一个原生函数（C 写的、被 JS 调用的函数）处理参数的典型流程是：

```
1. njs_argument(args, 0)            // 取第 0 个参数（this）
   njs_arg(args, nargs, 1)          // 取第 1 个参数，越界则返回 &undefined
2. if (!njs_is_number(arg1)) ...     // 类型测试
3. double x = njs_number(arg1);      // 读 payload
4. njs_value_assign(retval, &some);  // 写返回值
```

其中每一步都对应一个宏。这套流程在 `external/` 的扩展模块和 `nginx/` 的集成层里出现成百上千次，是 njs C 代码的基本语汇。

#### 4.3.3 源码精读

**A. 定位宏 `njs_argument`**

```c
/* sizeof(njs_value_t) is 16 bytes. */
#define njs_argument(args, n)                                                 \
    (njs_value_t *) ((u_char *) args + (n) * 16)
```

见 [src/njs.h:51-53](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs.h#L51-L53)。这就是 4.1.1 说的「固定 16 字节换指针算术」的直接体现：参数是一个连续的 16 字节数组，第 n 个参数就是起点加 `n*16` 字节。注释紧贴着 `sizeof(njs_value_t) is 16 bytes`，提醒你这里的 `16` 就是值大小。`njs_arg`（[njs.h:58-60](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs.h#L58-L60)）在它基础上加了「越界返回 `&njs_value_undefined`」的安全网。

**B. 拷贝宏 `njs_value_assign`——为什么是 `memcpy`**

```c
#define njs_value_assign(dst, src)                                            \
    memcpy(dst, src, sizeof(njs_opaque_value_t))
```

见 [src/njs.h:62-63](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs.h#L62-L63)。为什么不写 `*dst = *src`，而要用 `memcpy` 拷 16 字节？有三个理由：

1. **类型双关的安全性（type-punning）**。值的 16 字节会被不同视角写入和读出：写时可能用 `data` 视图（写 `u.number`），读时却可能用 `njs_value_atom`（把前 4 字节当 `uint32_t` 读，见下文 D）。C 标准里「通过非 active 成员访问联合体」属于未定义行为；而 `memcpy` 是标准规定的、对任意对象内存表示进行拷贝的**安全方式**，不依赖哪个成员是 active 的。所以 `njs_value_assign` 用 `memcpy` 是规避 strict-aliasing / 联合体双关带来的 UB。
2. **对齐安全**。有些值拷贝的目标并不保证对齐到 8 字节——比如 `njs_object_value_s.value` 与 `njs_regexp_s.string` 字段旁的注释就写明「This value can be unaligned since it never used in nJSVM operations」（见 [src/njs_value.h:181-182](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L181-L182) 与 [256-260](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L256-L260)）。结构体赋值在某些平台上可能生成要求对齐的指令；`memcpy` 对未对齐地址是可移植的。
3. **解耦内部布局**。拷贝的大小用 `sizeof(njs_opaque_value_t)`（= 16，见 [njs.h:47-49](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs.h#L47-L49)），而不是 `sizeof(njs_value_t)`。这让「拷贝值」这个动作只依赖那个不透明容器的大小，不依赖内部 union 的具体形态——外部代码拷值时完全不需要知道 union 长什么样。

一句话：`memcpy` 16 字节是「把一个可能被双关、可能未对齐的值，安全、可移植地搬走」的标准做法。

**C. 类型测试宏 `njs_is_*` 与真值读取**

测试宏已在 4.2.3 列过范围判断的一组，这里补充几个「按真值/有效性」的：

```c
#define njs_is_true(value)       ((value)->data.truth != 0)
#define njs_bool(value)          ((value)->data.truth)
#define njs_is_valid(value)      ((value)->type != NJS_INVALID)
```

见 [src/njs_value.h:481-482](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L481-L482)、[659-660](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L659-L660)、[655-656](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L655-L656)。`njs_is_true` 直接读预算好的 `truth`——这就是 4.1.3 那段 `truth` 注释的兑现：逻辑判断无需区分类型。`njs_is_valid` 用来排除 `NJS_INVALID` 哨兵（未初始化的数组槽就是这个类型，见枚举注释 [njs_value.h:44-49](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L44-L49)）。

**D. payload 读取宏与 `njs_value_atom`**

```c
#define njs_number(value)    ((value)->data.u.number)
#define njs_data(value)      ((value)->data.u.data)
#define njs_function(value)  ((value)->data.u.function)
#define njs_object(value)    ((value)->data.u.object)
#define njs_value_atom(val)  (((njs_opaque_value_t *) (val))->filler[0])
```

见 [src/njs_value.h:663-664](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L663-L664)、[667-668](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L667-L668)、[671-672](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L671-L672)、[679-680](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L679-L680) 与 [src/njs.h:66](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs.h#L66)。前四个是「按类型取 payload」的快捷方式。`njs_value_atom` 最有意思：它把值强转成不透明容器、读 `filler[0]`——也就是前 4 字节。结合 4.1 的布局，这前 4 字节对 string 是 `atom_id`、对 symbol 是 `magic32`（symbol id）。所以 `njs_value_atom` 是「从任意键值（string/symbol）里抽出它的 atom」的统一入口，属性查找 `njs_value_property_val` 就用它：

```c
njs_inline njs_int_t
njs_value_property_val(...) {
    if (njs_value_atom(key) == 0 /* NJS_ATOM_STRING_unknown */) {
        ret = njs_atom_atomize_key(vm, key);   // 还没驻留，先驻留
        ...
    }
    return njs_value_property(vm, value, key->atom_id, retval);
}
```

见 [src/njs.h:566-577](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs.h#L566-L577)（同模式另见 [njs_value.h:1003-1017](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L1003-L1017)）。逻辑是：如果键的 atom 还是 0（未驻留），先把它 atomize 进 atom 表，再用 `key->atom_id` 去查属性。这正是 u2-l4 Atom 表要展开的内容，这里先建立「atom_id 是属性键的统一货币」的直觉即可。

**E. 预定义常量**

引擎启动时就备好了一批只读的「公共值」常量，C 代码里需要 `null` / `undefined` / `true` / `false` / `0` / `NaN` / `invalid` 时直接取地址引用，不必每次构造：

```c
const njs_value_t  njs_value_null =         njs_value(NJS_NULL, 0, 0.0);
const njs_value_t  njs_value_undefined =    njs_value(NJS_UNDEFINED, 0, NAN);
const njs_value_t  njs_value_false =        njs_value(NJS_BOOLEAN, 0, 0.0);
const njs_value_t  njs_value_true =         njs_value(NJS_BOOLEAN, 1, 1.0);
const njs_value_t  njs_value_zero =         njs_value(NJS_NUMBER, 0, 0.0);
const njs_value_t  njs_value_nan =          njs_value(NJS_NUMBER, 0, NAN);
const njs_value_t  njs_value_invalid =      njs_value(NJS_INVALID, 0, 0.0);
```

见 [src/njs_value.c:25-31](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.c#L25-L31)。它们都用 `njs_value(type, truth, number)` 宏按 `data` 视图初始化：

```c
#define njs_value(_type, _truth, _number) (njs_value_t) {                     \
    .data = { .type = _type, .truth = _truth, .u.number = _number }           \
}
```

见 [src/njs_value.h:367-373](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L367-L373)。注意每个常量的 `truth` 都符合 JS 语义：`null/undefined/false/0/NaN` 的 truth 是 0，`true` 的 truth 是 1，`invalid` 的 truth 是 0。于是一组 `njs_set_undefined` / `njs_set_true` 宏（[njs_value.h:743-756](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L743-L756)）就是直接把对应常量整块拷过去。

顺带一提，`njs_value.c` 还提供了一组「外部友好」的包装函数（`njs_value_number` 返回 double、`njs_value_is_string` 等，见 [njs_value.c:425-590](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.c#L425-L590)），它们内部就是调用本模块这些宏——是给不依赖内部布局的外部代码用的薄封装。

#### 4.3.4 代码实践

**实践目标**：在真实源码里追踪一次「取参数 → 测类型 → 读 payload → 拷返回值」的完整调用，把本模块的宏串起来。

**操作步骤**：

1. 打开 [src/njs_value.c:432-443](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.c#L432-L443) 的 `njs_value_number`：它返回 `njs_number(value)`——印证「读取宏 = 取 `data.u.number`」。
2. 在 [src/njs_value.c:1062-1096](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.c#L1062-L1096) 的 `njs_value_property` 数组快路径里找到 `njs_value_assign(retval, &array->start[index])`——印证「拷贝宏 = memcpy 16 字节」。
3. 在 [src/njs_value.c:493-527](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.c#L493-L527) 的 `njs_value_is_valid_number` 里看到 `njs_is_number(value) && !isnan(njs_number(value))`——印证「测试宏 + 读取宏」的组合用法。

**需要观察的现象**：

- 每个宏都极短（一行），背后是 4.1/4.2 讲的布局与类型设计；
- 代码从不直接写 `value->data.u.xxx`，而是用宏，意图清晰且类型安全。

**预期结果**：你能指着任意一段 njs C 代码说出「这一行用的是定位/拷贝/读取/测试哪类宏，它对应值的哪几个字节」。

> 待本地验证（可选）：在 `external/` 下任选一个原生函数（例如 `njs_fs_module.c` 里的某个方法），数一数它的函数体里出现了多少次 `njs_arg` / `njs_is_*` / `njs_value_assign`，体会这套工具宏在真实代码里的密度。

#### 4.3.5 小练习与答案

**练习 1**：`njs_value_assign(dst, src)` 用 `sizeof(njs_opaque_value_t)` 而不是 `sizeof(njs_value_t)`，除了「大小相同」之外，对代码可维护性有什么好处？

**参考答案**：`njs_opaque_value_t` 是对外暴露的不透明类型，外部代码（NGINX 模块、用户扩展）只认得它、看不到内部 union。用它的 size 做拷贝，意味着「拷贝一个值」这件事**不依赖内部实现细节**——即便将来 njs 内部把 union 改成别的布局，只要不透明容器仍是 16 字节，所有用 `njs_value_assign` 的外部代码都不用改。这是一种封装。

**练习 2**：`njs_value_atom(val)` 读的是 `filler[0]`（前 4 字节）。对一个 `NJS_NUMBER` 值调用它会得到什么？对一个 `NJS_SYMBOL` 值呢？

**参考答案**：对 `NJS_NUMBER` 值，前 4 字节是 `atom_id`，而 `njs_set_number` 把它清成了 0（[njs_value.h:793](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L793)），所以 `njs_value_atom` 返回 0（即 `NJS_ATOM_STRING_unknown`，表示「没有 atom」）。对 `NJS_SYMBOL` 值，前 4 字节是 `magic32` = symbol 的 id（[njs_value.h:824](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L824)），所以返回该 symbol 的 id。这正是 `magic32` 与 `atom_id` 物理重叠带来的统一读法。

**练习 3**：`njs_value_true`、`njs_value_false`、`njs_value_undefined` 为什么被声明成 `const` 全局变量、到处取地址引用，而不是每次需要时现场构造一个？

**参考答案**：因为这些值是**不可变且全局唯一**的（JS 语义里 `true` 永远是同一个 `true`）。声明成 `const` 全局变量后：(1) 取地址 `&njs_value_true` 得到的是一个稳定的、可被多处共享引用的指针（比如 `njs_arg` 越界时就返回 `&njs_value_undefined`，见 [njs.h:58-60](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs.h#L58-L60)）；(2) 避免了在栈上反复构造同样 16 字节的浪费；(3) `const` 还让编译器把它们放进只读段，利于缓存与安全。

---

## 5. 综合实践

把三个模块串起来，完成下面这个「**给一段 C 代码逐字段解读值**」的综合任务。

**任务**：阅读下面这段（节选自 `njs_set_number` 与 `njs_set_symbol` 思路的）示意写法，对照 4.1 的布局表与 4.2 的类型表，画出执行后这 16 字节的内存示意图，并回答问题。

```c
njs_value_t  v;
njs_set_number(&v, 3.14);
/* 此时 v 的 16 字节是怎样的？ */

njs_set_symbol(&v, NJS_ATOM_SYMBOL_iterator, &name);
/* 改写后 v 的 16 字节又是怎样的？ */
```

**要求你的示意图至少标出**：

1. `njs_set_number(&v, 3.14)` 之后：
   - 字节 0–3（`atom_id`）：`0`；
   - 字节 4（`type`）：`NJS_NUMBER`(3)；
   - 字节 5（`truth`）：`1`（因为 3.14 非零非 NaN）；
   - 字节 8–15（`u.number`）：`3.14` 的 IEEE 754 表示。
2. `njs_set_symbol(...)` 之后：
   - 字节 0–3（`magic32`）：`NJS_ATOM_SYMBOL_iterator`（symbol id）；
   - 字节 4（`type`）：`NJS_SYMBOL`(4)；
   - 字节 5（`truth`）：`1`；
   - 字节 8–15（`u.value`）：指向描述串 `name` 的指针。

**进阶思考**：

3. 两次写之间，**哪些字节被改动了、哪些没变？** 这能说明「同一段 16 字节被不同视角复用」的什么特点？（提示：`type` 总在第 4 字节、`truth` 总在第 5 字节、payload 总在后 8 字节——头部布局稳定，只有「头部第 0–3 字节的含义」和「payload 的解释方式」随类型切换。）
4. 如果现在对 `v` 调用 `njs_value_atom(&v)`，两次分别得到什么？为什么？（答：number 时得 0，symbol 时得 `NJS_ATOM_SYMBOL_iterator`，因为 `njs_value_atom` 读的前 4 字节正是 `atom_id`/`magic32`。）

> 这个综合实践以源码阅读 + 画内存图为主，不需要运行。鼓励你构建 `build/njs` 后，用 `./build/njs -d -c 'var s = Symbol.iterator; var n = 3.14;'` 反汇编，观察 `Symbol.iterator` 这种 symbol 在字节码层就是以一个整数 id 出现的，印证本讲讲的「symbol id 存在前 4 字节」。

---

## 6. 本讲小结

- `njs_value_t` 是 njs 里**唯一**的 JS 值类型，固定 **16 字节**；同一段 16 字节有 `data` / `string` / 匿名三种重叠视图，头部（前 8 字节：`atom_id`/`magic32` + `type` + `truth` + `magic16`）共享，payload（后 8 字节：`u` 联合）随类型变化（[njs_value.h:93-140](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L93-L140)）。
- 类型枚举 `njs_value_type_t` 被刻意排序：原始值在 `0..NJS_STRING`、对象类从 `0x10` 起，于是 `njs_is_primitive`(`<=NJS_STRING`)、`njs_is_object`(`>=NJS_OBJECT`)、`njs_is_numeric`(`<=NJS_NUMBER`) 等都能用一个范围比较完成（[njs_value.h:16-64](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L16-L64)）。
- 不同类型的 payload 不同：number 存 `double` 于 `u.number`（且 `atom_id=0`）、symbol 把 id 存进 `magic32`、string/对象/函数/外部数据存指针；`truth` 在写入时按 JS 语义预算好，逻辑判断只读它一个字节。
- **atom 双关**：前 4 字节既是 `atom_id`（string）又是 `magic32`（symbol/data）；属性键用 32 位 `atom_id` 传递，并用最高位 `0x80000000` 把整数键直接编码进去（`njs_number_atom`/`njs_atom_is_number`/`njs_atom_number`），使数组下标无需驻留成字符串（[njs.h:68-70](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs.h#L68-L70)、[njs_value.c:1059-1097](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.c#L1059-L1097)、[njs_number.h:181-193](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_number.h#L181-L193)）。
- 日常工具宏四类：定位（`njs_argument` = `args + n*16`）、拷贝（`njs_value_assign` = `memcpy` 16 字节，用 memcpy 是为类型双关安全 + 对齐安全 + 解耦内部布局）、读取（`njs_number`/`njs_object`/`njs_value_atom` 等）、测试（`njs_is_*`）；外加 `njs_value_null/undefined/true/false/...` 一组只读预定义常量（[njs_value.c:25-31](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.c#L25-L31)）。

---

## 7. 下一步学习建议

本讲你掌握了「值的形状」。接下来要回答的是「这些值被存放在哪里、用什么数据结构组织」。建议按以下顺序继续：

1. **u2-l3（内存池 `njs_mp` 与 `njs_flathsh` 哈希表）**：本讲的预定义常量、symbol 描述串、对象结构体都住在 `vm->mem_pool` 里；对象的可变属性则存在 `njs_flathsh_t`。下一讲讲清池式分配与扁平哈希，你就能理解「一个值的生命周期由谁管理」。
2. **u2-l4（Atom 表）**：本讲反复出现的 `atom_id`、`NJS_ATOM_STRING_unknown`、`njs_atom_atomize_key`、`atom_hash_shared` vs 私有 `atom_hash`，下一讲给出完整的 atom 驻留机制。这是理解字符串/标识符为何能被高效比较的关键。
3. **想看值在「执行」时如何被搬运**：可以提前翻一眼 [src/njs_vmcode.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.c) 的解释器主循环——每条指令的操作数都是 `njs_value_t`，本讲学的布局与访问宏在那里被高频使用。完整理解建议留到 u4 单元。
4. **想看值在「属性查找」时如何被读写**：本讲引用的 [njs_value.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.c) 的 `njs_property_query` / `njs_value_property` 已经是属性系统的核心，u5-l1（对象模型）会沿着 `njs_object_t` 的双哈希 + 原型链把它讲透。
