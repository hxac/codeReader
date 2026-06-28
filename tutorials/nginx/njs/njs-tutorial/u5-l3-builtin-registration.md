# 内建对象与构造器的注册

## 1. 本讲目标

在前一讲（u5-l1）里，我们已经看清了 njs 的对象模型：每个 `njs_object_t` 同时持有一张只读的 `shared_hash`（装内建方法）和一张私有的 `hash`（装自身属性），并沿 `__proto__` 串成原型链。本讲回答下一个自然问题——

> 这成百上千个内建方法、几十个构造器（`Object`/`Array`/`Promise`/…）和原型，到底是**什么时候、由谁、按什么规则**装进引擎的？它们为什么能被「一次创建、所有 VM 克隆复用」？

读完本讲，你应当能够：

- 说清 `NJS_OBJ_TYPE_*` 这一组枚举是如何给「每一种内建类型」编号、分段的，以及编号如何当数组下标使用；
- 读懂两张「声明驱动的初始化表」`njs_object_init[]` 与 `njs_object_type_init[]`，理解「用静态表描述、用循环物化」的设计；
- 描述 `njs_builtin_objects_create` 如何把模板 VM 的 `shared` 装满，以及克隆 VM 如何只读复用 `shared`、却各自重建可写的 `constructors`/`prototypes` 副本。

## 2. 前置知识

本讲假设你已经掌握 u5-l1 的内容，这里再点出三个最相关的概念：

- **`shared_hash` 与 `hash`**：内建方法挂在只读的 `shared_hash` 上，自身属性写在私有的 `hash` 上。本讲的全部「注册」工作，本质上就是**往某张 `shared_hash` 里塞属性描述符**。
- **`njs_flathsh_t` 扁平哈希表**（u2-l3）：属性的键是 32 位 `atom_id`（u2-l4），`njs_flathsh_unique_insert` 按 `atom_id` 直接做整数比较插入。
- **`njs_vm_t` 与 `njs_vm_shared_t`**：一个 VM 持有一个 `shared` 指针（`vm->shared`）。`shared` 是「跨克隆只读共享」的大资源包；`vm` 自身则持有可写副本。这条「模板 vs 副本」的边界正是本讲的主线。

另外补充两个术语，避免后续混淆：

- **构造器（constructor）**：像 `Array`、`Promise` 这样的「函数对象」，调用它（或 `new` 它）会产出一个实例。在 C 层它是一个 `njs_function_t`。
- **原型（prototype）**：与构造器一一对应的「原型对象」（`Array.prototype`、`Promise.prototype`），实例共享其上的方法。在 C 层它是一个 `njs_object_prototype_t` 联合体。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/njs_vm.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.h) | 定义 `njs_object_type_t` 枚举（`NJS_OBJ_TYPE_*`）、`njs_vm_shared_t` 结构，以及 `njs_shared_ctor`/`njs_shared_prototype` 访问宏 |
| [src/njs_value.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h) | 定义 `njs_object_type_init_t`（每个类型的「构造器 + 原型属性 + 原型值」三元组）与 `njs_object_prototype_t` 联合体 |
| [src/njs_object.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_object.h) | 定义 `njs_object_init_t`（一张属性表 = 指针 + 条数）与 `njs_object_hash_create` 声明 |
| [src/njs_object_prop_declare.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_object_prop_declare.h) | `NJS_DECLARE_PROP_VALUE/NATIVE/HANDLER` 三个声明宏——写初始化表的主力工具 |
| [src/njs_builtin.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_builtin.c) | **本讲核心**：`njs_object_init[]`、`njs_object_type_init[]` 两张表，以及物化函数 `njs_builtin_objects_create` |
| [src/njs_vm.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c) | `njs_vm_create` 决定是否建 `shared`、`njs_vm_ctor_push` 给 `shared` 数组扩容、`njs_vm_protos_init` 把 `shared` 拷成 per-VM 副本、`njs_vm_constructors_init` 连原型链 |
| [src/njs_object.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_object.c) | `njs_object_hash_create`（把一张声明表真正插进哈希）、`njs_obj_type_init`（Object 类型的初始化三元组示例） |

---

## 4. 核心概念与源码讲解

### 4.1 对象类型枚举 NJS_OBJ_TYPE_*

#### 4.1.1 概念说明

njs 把「所有需要构造器 + 原型的内建类型」排进**同一个枚举** `njs_object_type_t`，每个类型得到一个从 0 开始的整数编号。这个编号不是摆设——它**直接当成数组下标**用：

- `shared->constructors[index]` 取这个类型的构造器；
- `shared->prototypes[index]` 取这个类型的原型；
- `vm->constructors[index]` / `vm->prototypes[index]` 取 per-VM 副本。

所以「`Array` 是第几号类型」必须有**编译期固定**的答案，否则下标就乱了。这套编号就是那个固定答案。

#### 4.1.2 核心流程

枚举被刻意分成四段（用 `#define` 边界宏标注），便于其它代码按段批量处理：

```
┌─ Global types (普通内建类型) ─────────── OBJECT..BUFFER
├─ Hidden types (不暴露给 JS 的内部类型) ── ITERATOR, ARRAY_ITERATOR, TYPED_ARRAY
├─ TypedArray types (9 种 TypedArray) ──── UINT8_ARRAY..FLOAT64_ARRAY
└─ Error types (Error 家族) ─────────────── ERROR..AGGREGATE_ERROR
                                                   ↑ NJS_OBJ_TYPE_MAX 哨兵
```

分段的意义：例如 `njs_vm_constructors_init` 会把「所有 TypedArray 类型的原型」统一接到 `TypedArray.prototype` 后面、把「所有 Error 子类型的原型」统一接到 `Error.prototype` 后面——靠的就是「它们在枚举里连续」这件事。

#### 4.1.3 源码精读

枚举本体在 [src/njs_vm.h:22-82](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.h#L22-L82)。注意它如何用注释和宏把四段隔开：

```c
typedef enum {
    NJS_OBJ_TYPE_OBJECT = 0,
    NJS_OBJ_TYPE_ARRAY,
    ...
    NJS_OBJ_TYPE_BUFFER,          // 普通段结束

#define NJS_OBJ_TYPE_HIDDEN_MIN    (NJS_OBJ_TYPE_ITERATOR)
    NJS_OBJ_TYPE_ITERATOR,
    NJS_OBJ_TYPE_ARRAY_ITERATOR,
    NJS_OBJ_TYPE_TYPED_ARRAY,
#define NJS_OBJ_TYPE_HIDDEN_MAX    (NJS_OBJ_TYPE_TYPED_ARRAY + 1)
...
    NJS_OBJ_TYPE_UINT8_ARRAY,
    ...
    NJS_OBJ_TYPE_FLOAT64_ARRAY,
#define NJS_OBJ_TYPE_TYPED_ARRAY_MAX    (NJS_OBJ_TYPE_FLOAT64_ARRAY + 1)
...
    NJS_OBJ_TYPE_ERROR,
    ...
    NJS_OBJ_TYPE_AGGREGATE_ERROR,
#define NJS_OBJ_TYPE_ERROR_MAX         (NJS_OBJ_TYPE_AGGREGATE_ERROR)

    NJS_OBJ_TYPE_MAX,             // 总数哨兵
} njs_object_type_t;
```

几个关键点：

- `NJS_OBJ_TYPE_OBJECT = 0`：下标从 0 开始，与数组自然对齐。
- 各种 `*_MIN`/`*_MAX` 宏是**段边界**，供循环用，例如 `for (i = NJS_OBJ_TYPE_TYPED_ARRAY_MIN; i < NJS_OBJ_TYPE_TYPED_ARRAY_MAX; i++)`。
- `NJS_OBJ_TYPE_MAX` 是「类型总数 + 1」的哨兵，等于 38（普通 16 + 隐藏 3 + TypedArray 9 + Error 10）。`njs_object_type_init[]` 数组就按它定长。

另外两个小工具宏也在同一个头里，专门处理「编号 ↔ 原型」的换算（[src/njs_vm.h:85-90](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.h#L85-L90)）：

```c
#define njs_primitive_prototype_index(type)  (NJS_OBJ_TYPE_BOOLEAN + ((type) - NJS_BOOLEAN))
#define njs_prototype_type(index)            (index + NJS_OBJECT)
```

它们把「值类型（如 `NJS_BOOLEAN`）」与「对应的包装对象类型（`NJS_OBJ_TYPE_BOOLEAN`）」互相换算，给布尔/数字/字符串原型用。

#### 4.1.4 代码实践

**目标**：亲手清点 `NJS_OBJ_TYPE_*` 的全部成员，确认「枚举值 = 数组下标」这件事。

**步骤**：

1. 打开 [src/njs_vm.h:22-82](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.h#L22-L82)。
2. 从 `NJS_OBJ_TYPE_OBJECT = 0` 开始，按 C 枚举隐式递增规则，给每个成员标上数值。
3. 数一下各段成员数，验证 `NJS_OBJ_TYPE_MAX` 是否等于 16+3+9+10 = 38。
4. 再去 [src/njs_builtin.c:42-93](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_builtin.c#L42-L93) 看 `njs_object_type_init[]` 数组——它的下标 0..37 是否正好按上面枚举顺序填入对应的 `*_type_init`。

**预期结果**：你会得到一张完整的类型编号表（见 4.1.5 的答案）。关键是体会到：**改枚举顺序等于搬动所有内建类型的位置**，所以这套顺序在工程上是冻结的。

**待本地验证**：用编辑器折叠或脚本统计，确认 `njs_object_type_init[]` 数组恰好 38 项、与枚举一一对应。

#### 4.1.5 小练习与答案

**Q1**：`NJS_OBJ_TYPE_PROMISE` 的数值是多少？

**答**：它是普通段的第 11 个（`OBJECT=0, ARRAY=1, BOOLEAN=2, NUMBER=3, SYMBOL=4, STRING=5, FUNCTION=6, ASYNC_FUNCTION=7, REGEXP=8, DATE=9, PROMISE=10`），所以是 10。

**Q2**：为什么 Error 子类要放在枚举最后一段、且连续排列？

**答**：因为它们的原型链都长一样——`TypeError.prototype.__proto__ === Error.prototype`。`njs_vm_constructors_init` 用一个 `for (i = NJS_OBJ_TYPE_EVAL_ERROR; i < constructors_size; i++)` 循环，把这一整段原型的 `__proto__` 统一指向 `Error.prototype`。连续排列让这种「批量接线」可以用一次循环完成。

---

### 4.2 内建对象初始化表：用声明驱动物化

#### 4.2.1 概念说明

njs 注册内建有一个统一的套路：**不在 C 代码里一条条手写「插入属性」，而是先写一张静态的「属性声明表」，再用一个循环把表物化进哈希**。这样做的好处是——所有内建方法集中可见、易于维护，新增一个内建函数只需在表里加一行声明宏。

这里有两类「声明表」，对应两类内建：

| 表 | 类型 | 描述谁 | 物化进哪里 |
|---|---|---|---|
| `njs_object_init[]` | `njs_object_init_t` | **没有构造器的单例对象**（`globalThis`/`njs`/`process`/`Math`/`JSON`） | `shared->objects[NJS_OBJECT_MAX]` |
| `njs_object_type_init[]` | `njs_object_type_init_t` | **有构造器+原型的类型**（38 种） | `shared->constructors[]` + `shared->prototypes[]` |

注意第二张表是「按 `NJS_OBJ_TYPE_*` 下标」排列的——这把 4.1 的枚举与本节的物化直接绑定。

#### 4.2.2 核心流程

先看最小数据结构，理解「一张表」长什么样：

```c
// 一条属性声明：atom_id（属性名）+ 类型 + 值 + 可枚举/可配置/可写
struct njs_object_init_s {              // src/njs_object.h
    const njs_object_prop_init_t  *properties;   // 指向静态数组
    njs_uint_t                     items;        // 条数
};

// 一个类型的「构造器 + 原型」全套描述
struct njs_object_type_init_s {        // src/njs_value.h
    njs_function_t            constructor;        // 构造器函数模板
    const njs_object_init_t  *constructor_props;  // 构造器自己的属性（如 Array.isArray）
    const njs_object_init_t  *prototype_props;    // 原型上的属性（如 Array.prototype.push）
    njs_object_prototype_t    prototype_value;    // 原型的「壳」（类型标记等）
};
```

写表靠三个声明宏（[src/njs_object_prop_declare.h:10-39](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_object_prop_declare.h#L10-L39)），它们把一行声明展开成一条 `njs_object_prop_init_t`：

- `NJS_DECLARE_PROP_VALUE(name, value, flags)`：普通值属性（`name = value`）。
- `NJS_DECLARE_PROP_NATIVE(name, fn, nargs, magic)`：原生函数属性（`name(args)` 调到 C 函数 `fn`）。
- `NJS_DECLARE_PROP_HANDLER(name, fn, magic16, flags)`：属性处理器（GET/SET/DELETE 都走同一个 C 回调 `fn`，用于惰性求值）。

物化靠一个函数 `njs_object_hash_create`（[src/njs_object.c:158-193](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_object.c#L158-L193)）：遍历声明表，对每条调用 `njs_flathsh_unique_insert`，以 `atom_id` 为键插进目标哈希。

#### 4.2.3 源码精读

**单例对象表** `njs_object_init[]`（[src/njs_builtin.c:32-39](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_builtin.c#L32-L39)）——只有 5 项，以 `NULL` 结尾：

```c
static const njs_object_init_t  *njs_object_init[] = {
    &njs_global_this_init,
    &njs_njs_object_init,
    &njs_process_object_init,
    &njs_math_object_init,
    &njs_json_object_init,
    NULL
};
```

**类型表** `njs_object_type_init[]`（[src/njs_builtin.c:42-93](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_builtin.c#L42-L93)）——按 `NJS_OBJ_TYPE_*` 顺序、定长 `NJS_OBJ_TYPE_MAX` 项：

```c
static const njs_object_type_init_t *const
    njs_object_type_init[NJS_OBJ_TYPE_MAX] =
{
    /* Global types. */
    &njs_obj_type_init,        // 对应 NJS_OBJ_TYPE_OBJECT
    &njs_array_type_init,      // 对应 NJS_OBJ_TYPE_ARRAY
    ...
    &njs_buffer_type_init,
    /* Hidden types. */
    &njs_iterator_type_init,
    ...
    /* TypedArray types. */
    &njs_typed_array_u8_type_init,
    ...
    /* Error types. */
    &njs_error_type_init,      // 对应 NJS_OBJ_TYPE_ERROR
    ...
    &njs_aggregate_error_type_init,
};
```

这两张表只是「指针的集合」——真正的属性声明表（`njs_global_this_object_properties[]`、`njs_object_prototype_init` 等）散落在各自模块的 `.c` 文件里。以 Object 类型为例，它的三元组在 [src/njs_object.c:2821-2826](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_object.c#L2821-L2826)：

```c
const njs_object_type_init_t  njs_obj_type_init = {
    .constructor = njs_native_ctor(njs_object_constructor, 1, 0),
    .constructor_props = &njs_object_constructor_init,   // Object.keys / Object.create ...
    .prototype_props = &njs_object_prototype_init,        // Object.prototype.toString ...
    .prototype_value = { .object = { .type = NJS_OBJECT } },
};
```

而声明表本身用宏写成，例如 globalThis 上的全局函数（[src/njs_builtin.c:649-657](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_builtin.c#L649-L657)）：

```c
NJS_DECLARE_PROP_NATIVE(STRING_isFinite, njs_number_global_is_finite, 1, 0),
NJS_DECLARE_PROP_NATIVE(STRING_isNaN,     njs_number_global_is_nan, 1, 0),
NJS_DECLARE_PROP_NATIVE(STRING_parseInt,  njs_number_parse_int, 2, 0),
```

每行声明一个内建函数（名字 atom、C 实现、形参个数、magic）。物化时，`njs_object_hash_create` 把它们逐条插进哈希。

#### 4.2.4 代码实践

**目标**：跟踪「一行声明 → 一条哈希条目」的完整路径。

**步骤**：

1. 在 [src/njs_builtin.c:848](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_builtin.c#L848) 找到 `njs.dump` 的声明：`NJS_DECLARE_PROP_NATIVE(STRING_dump, njs_ext_dump, 0, 0)`。
2. 把它对照 [src/njs_object_prop_declare.h:23-26](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_object_prop_declare.h#L23-L26) 的 `NJS_DECLARE_PROP_NATIVE` 宏，手动展开：`atom_id = NJS_ATOM_STRING_dump`、`type = NJS_PROPERTY`、`u.value` 是由 `njs_native_function2(njs_ext_dump, 0, 0)` 造出的函数对象。
3. 这条声明属于 `njs_njs_object_properties[]`，最终被 `njs_object_hash_create` 插进 `njs` 对象的 `shared_hash`（见 [src/njs_builtin.c:207-220](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_builtin.c#L207-L220) 的循环）。
4. 跟到 `njs_object_hash_create`（[src/njs_object.c:170-190](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_object.c#L170-L190)）：`fhq.key_hash = prop->desc.atom_id` 后 `njs_flathsh_unique_insert`。

**预期结果**：你看清「声明宏 → 静态数组项 → 循环里插入哈希」三步，没有任何运行期字符串拼装，全是编译期数据。

**待本地验证**：可选——构建 CLI 后运行 `./build/njs -e 'console.log(typeof njs.dump)'`，应输出 `function`，验证声明确实被注册了。

#### 4.2.5 小练习与答案

**Q1**：`NJS_DECLARE_PROP_VALUE`、`NJS_DECLARE_PROP_NATIVE`、`NJS_DECLARE_PROP_HANDLER` 三者最本质的区别是什么？

**答**：区别在生成的属性类型与「何时求值」。`VALUE` 生成 `NJS_PROPERTY`，值在编译期就定死；`NATIVE` 是 `VALUE` 的特例，值是一个原生函数对象；`HANDLER` 生成 `NJS_PROPERTY_HANDLER`，**没有预存值**，每次访问属性都调用 C 回调动态求值（适合 `njs`/`process`/globalThis 这类需要惰性或带副作用的属性）。

**Q2**：为什么 `njs_object_init[]` 用 `NULL` 结尾、而 `njs_object_type_init[]` 用定长 `NJS_OBJ_TYPE_MAX`？

**答**：因为单例对象表是「按顺序遍历」的（`for (p = njs_object_init; *p != NULL; p++)`），下标不重要，用 NULL 结尾最省事；而类型表要被「按下标随机访问」（`njs_object_type_init[i]`，`i` 就是 `NJS_OBJ_TYPE_*`），所以必须定长且顺序与枚举严格一致。

---

### 4.3 shared 构造器/原型：njs_builtin_objects_create 与跨克隆复用

#### 4.3.1 概念说明

前两节是「数据怎么声明」，本节是「数据怎么变成运行时对象」。核心函数是 `njs_builtin_objects_create`，它只在**创建模板 VM 时调用一次**，把 `njs_vm_shared_t`（`vm->shared`）整个装满。

为什么要把这些放进 `shared` 而不是 `vm`？回到 u2-l1 的克隆模型：NGINX 每来一个请求都要 `njs_vm_clone` 出一个新 VM。如果每个克隆都重新物化 38 个构造器、38 个原型、几百个内建方法，开销惊人。所以 njs 的策略是：

- **`shared` 是只读模板**：构造器/原型/各实例方法表只造一份，所有克隆共享同一个 `shared` 指针。
- **`vm->constructors` / `vm->prototypes` 是可写副本**：因为每个 VM 要在原型上接自己的原型链（`__proto__`）、globalThis 要可写，所以每个克隆从 `shared` `memcpy` 一份出来再加工。

这就是「shared 层惰性共享、per-VM 按需物化」的全部含义。

#### 4.3.2 核心流程

`njs_builtin_objects_create` 的执行顺序（[src/njs_builtin.c:129-299](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_builtin.c#L129-L299)）：

```
1. 分配 njs_vm_shared_t，挂到 vm->shared
2. 初始化 atom 表、values_hash、空正则模式
3. 物化 7 张「实例方法哈希」(array/string/function/async_function/arrow/arguments/regexp instance)
   → 存进 shared->xxx_instance_hash
4. 物化 5 个单例对象 (njs_object_init[]) → shared->objects[0..4] 的 shared_hash
5. 物化 env_hash (把进程环境变量塞进去)
6. 循环 NJS_OBJ_TYPE_OBJECT .. NJS_OBJ_TYPE_MAX：
   a. njs_vm_ctor_push：给 shared->constructors[] / prototypes[] 各扩一格
   b. 填 prototype：从 njs_object_type_init[i]->prototype_value 拷值，
      再物化 prototype_props 进 prototype->object.shared_hash
   c. 填 constructor：从 njs_object_type_init[i]->constructor 拷值，
      再物化 constructor_props 进 constructor->object.shared_hash
7. 设置 globalThis 的 prop_handler、global_object、string_object
8. 初始化 modules_hash
```

之后，每次 `njs_vm_create`（带 `init`）或 `njs_vm_clone`，会跑 `njs_vm_protos_init`：把 `shared->constructors/prototypes` 的内容 `memcpy` 成 per-VM 的 `vm->constructors/prototypes`，再调 `njs_vm_constructors_init` 在副本上接原型链。

> 关键不变量（[src/njs_builtin.c:233](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_builtin.c#L233) 的断言）：`njs_vm_ctor_push` 返回的下标 **必须等于** 当前的 `NJS_OBJ_TYPE_*` 值。正因为这个一一对应，构造器数组才能用类型编号当索引。

#### 4.3.3 源码精读

**① 物化单例对象**（[src/njs_builtin.c:205-220](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_builtin.c#L205-L220)）：

```c
object = shared->objects;
for (p = njs_object_init; *p != NULL; p++) {
    obj = *p;
    ret = njs_object_hash_init(vm, &object->shared_hash, obj);  // 填属性
    ...
    object->type = NJS_OBJECT;
    object->shared = 1;          // 标记为只读共享
    object->extensible = 1;
    object++;
}
```

`object->shared = 1` 是 u5-l1 写时复制机制要检查的标志——共享对象首次被写时，会先 `njs_prop_private_copy` 物化副本。

**② 物化 38 个构造器 + 原型**（[src/njs_builtin.c:227-277](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_builtin.c#L227-L277)）。先扩容、填原型：

```c
for (i = NJS_OBJ_TYPE_OBJECT; i < NJS_OBJ_TYPE_MAX; i++) {
    index = njs_vm_ctor_push(vm);              // 扩一格，返回下标
    njs_assert_msg((njs_uint_t) index == i, "ctor index should match object type");

    prototype = njs_shared_prototype(shared, i);
    *prototype = njs_object_type_init[i]->prototype_value;   // 拷原型「壳」

    /* boolean/number/string 原型要带上原始值（如 Boolean.prototype 的 false） */
    if (njs_object_type_init[i] == &njs_boolean_type_init) {
        prototype->object_value.value = njs_value(NJS_BOOLEAN, 0, 0.0);
    } ...

    ret = njs_object_hash_init(vm, &prototype->object.shared_hash,
                               njs_object_type_init[i]->prototype_props);  // 填原型方法
    prototype->object.extensible = 1;
}
```

再填构造器（注意 `constructor_props == NULL` 的「隐藏类型」会被跳过、仅清零，见 [src/njs_builtin.c:261-277](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_builtin.c#L261-L277)）：

```c
for (i = NJS_OBJ_TYPE_OBJECT; i < NJS_OBJ_TYPE_MAX; i++) {
    constructor = njs_shared_ctor(shared, i);
    if (njs_object_type_init[i]->constructor_props == NULL) {
        njs_memzero(constructor, sizeof(njs_function_t));   // 隐藏类型无构造器
        continue;
    }
    *constructor = njs_object_type_init[i]->constructor;
    constructor->object.shared = 0;                         // 构造器要可写
    ret = njs_object_hash_init(vm, &constructor->object.shared_hash,
                               njs_object_type_init[i]->constructor_props);
}
```

**③ `shared` 的字段全景**（[src/njs_vm.h:207-237](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.h#L207-L237)）：

```c
struct njs_vm_shared_s {
    njs_flathsh_t  values_hash;
    njs_flathsh_t  array_instance_hash;        // ← 7 张实例方法哈希
    njs_flathsh_t  string_instance_hash;
    njs_flathsh_t  function_instance_hash;
    njs_flathsh_t  async_function_instance_hash;
    njs_flathsh_t  arrow_instance_hash;
    njs_flathsh_t  arguments_object_instance_hash;
    njs_flathsh_t  regexp_instance_hash;
    njs_flathsh_t  modules_hash;
    njs_flathsh_t  env_hash;                   // 环境变量
    njs_object_t   string_object;
    njs_object_t   objects[NJS_OBJECT_MAX];    // ← 5 个单例对象
    njs_arr_t     *constructors;               // ← 38 个构造器
    njs_arr_t     *prototypes;                 // ← 38 个原型
    njs_exotic_slots_t  global_slots;
    njs_regexp_pattern_t *empty_regexp_pattern;
};
```

访问宏（[src/njs_vm.h:225-229](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.h#L225-L229)）就是按下标取 `njs_arr_t` 元素：

```c
#define njs_shared_ctor(shared, index)     ((njs_function_t *) njs_arr_item((shared)->constructors, index))
#define njs_shared_prototype(shared, index)((njs_object_prototype_t *) njs_arr_item((shared)->prototypes, index))
```

**④ 跨克隆复用**：`njs_vm_create` 决定是否建 `shared`（[src/njs_vm.c:60-68](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L60-L68)）——若调用者传了 `options->shared`（即克隆场景）就直接复用，否则才调 `njs_builtin_objects_create` 从零建一份：

```c
if (options->shared != NULL) {
    vm->shared = options->shared;          // 克隆：共享模板的 shared
} else {
    ret = njs_builtin_objects_create(vm);  // 模板：从零物化
    ...
}
```

而 `njs_vm_clone`（[src/njs_vm.c:391-466](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L391-L466)）做的是 `*nvm = *vm`（浅拷贝，于是 `nvm->shared` 与模板共享），随后 `njs_vm_protos_init` 重新建 per-VM 的 `constructors/prototypes`。

**⑤ per-VM 副本与原型链接线**：`njs_vm_protos_init`（[src/njs_vm.c:576-609](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L576-L609)）从 `shared` 拷贝：

```c
vm->constructors_size = vm->shared->constructors->items;
vm->constructors = njs_mp_alloc(vm->mem_pool, ctor_size + proto_size);
vm->prototypes  = (njs_object_prototype_t *)((u_char*)vm->constructors + ctor_size);
memcpy(vm->constructors, vm->shared->constructors->start, ctor_size);  // 拷
memcpy(vm->prototypes,  vm->shared->prototypes->start,  proto_size);
njs_vm_constructors_init(vm);   // 在副本上接 __proto__ 链
```

`njs_vm_constructors_init`（[src/njs_vm.c:513-573](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L513-L573)）就是 4.1.2 提到的「按段批量接原型链」：把普通类型原型接到 `Object.prototype`、TypedArray 接到 `TypedArray.prototype`、Error 子类接到 `Error.prototype`、所有构造器接到 `Function.prototype`。

> 为什么不在 `shared` 里就把原型链接好，而要每个 VM 接一遍？因为原型链的「箭头」是可写的 `__proto__` 指针，属于实例状态；`shared` 必须保持纯只读、可被任意 VM 复用，所以接线只能在 per-VM 副本上做。

#### 4.3.4 代码实践

**目标**：列出全部 `NJS_OBJ_TYPE_*`，并指出 `njs_vm_shared_t` 里哪些哈希表是「跨克隆共享的实例哈希」。

**步骤 1 — 清点枚举（承接 4.1.4）**：按 [src/njs_vm.h:22-82](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.h#L22-L82) 得到下表：

| 段 | 成员（编号） |
|---|---|
| 普通类型 | OBJECT(0) ARRAY(1) BOOLEAN(2) NUMBER(3) SYMBOL(4) STRING(5) FUNCTION(6) ASYNC_FUNCTION(7) REGEXP(8) DATE(9) PROMISE(10) ARRAY_BUFFER(11) DATA_VIEW(12) TEXT_DECODER(13) TEXT_ENCODER(14) BUFFER(15) |
| 隐藏类型 | ITERATOR(16) ARRAY_ITERATOR(17) TYPED_ARRAY(18) |
| TypedArray | UINT8_ARRAY(19) UINT8_CLAMPED_ARRAY(20) INT8_ARRAY(21) UINT16_ARRAY(22) INT16_ARRAY(23) UINT32_ARRAY(24) INT32_ARRAY(25) FLOAT32_ARRAY(26) FLOAT64_ARRAY(27) |
| Error | ERROR(28) EVAL_ERROR(29) INTERNAL_ERROR(30) RANGE_ERROR(31) REF_ERROR(32) SYNTAX_ERROR(33) TYPE_ERROR(34) URI_ERROR(35) MEMORY_ERROR(36) AGGREGATE_ERROR(37) |
| 哨兵 | NJS_OBJ_TYPE_MAX(38) |

**步骤 2 — 区分 `shared` 里的哈希**：对照 [src/njs_vm.h:207-237](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.h#L207-L237)，`njs_vm_shared_t` 里**跨克隆共享**的哈希/对象有：

- **7 张实例方法哈希**（`array_instance_hash`、`string_instance_hash`、`function_instance_hash`、`async_function_instance_hash`、`arrow_instance_hash`、`arguments_object_instance_hash`、`regexp_instance_hash`）：这是「实例哈希」——每个 Array/String/Function 实例的 `shared_hash` 都指向它们，方法只存一份。
- 其它共享资源：`values_hash`（缓存的正则/字符串值）、`modules_hash`（已加载模块）、`env_hash`（环境变量）、`objects[5]`（5 个单例）、`constructors`/`prototypes`（38 个构造器/原型的模板）、`global_slots`、`empty_regexp_pattern`。

需要与 per-VM 的区分清楚：`vm->values_hash`、`vm->modules_hash`、`vm->atom_hash` 是**每个克隆私有**的（见 `njs_vm_runtime_init` 在 [src/njs_vm.c:477-510](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L477-L510) 里 `njs_flathsh_init` 重置它们）。

**预期结果**：你能用一句话回答「`shared` 里跨克隆共享的实例哈希是哪 7 张」——它们都是 `*_instance_hash`，挂在各类实例的 `shared_hash` 上。

#### 4.3.5 小练习与答案

**Q1**：`njs_vm_clone` 之后，新 VM 的 `Array.prototype.push` 这个方法存了几份？分别在哪？

**答**：方法本身只存一份，在 `shared->array_instance_hash`（被 Array 实例的 shared_hash 共享）；但「`Array.prototype`」这个原型对象，每个 VM 有一份自己的副本（`vm->prototypes[NJS_OBJ_TYPE_ARRAY]`，由 `njs_vm_protos_init` 从 shared 拷贝），副本的 `shared_hash` 仍指回 shared 的同一张实例表。所以「方法数据 1 份，原型壳每 VM 1 份」。

**Q2**：构造器物化时为什么要 `constructor->object.shared = 0`（[src/njs_builtin.c:270](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_builtin.c#L270)），而单例对象物化时却设 `object->shared = 1`？

**答**：单例对象（如 `Math`）的整张 shared_hash 在 shared 层只读共享，写它要触发写时复制，所以标 `shared = 1`。而构造器（如 `Array`）会被 globalThis 的 `njs_top_level_constructor` 处理器**按需物化进每个 VM 的全局对象哈希**（[src/njs_builtin.c:576-621](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_builtin.c#L576-L621)），且其上可被 JS 改写（如 `Array.isArray = ...`），故不能标记为共享只读。

---

## 5. 综合实践

**任务**：用本讲的三条主线，解释「`new Promise(...)` 这个表达式背后，引擎在启动期做了哪些准备」。

请按下列顺序产出一张「时序 + 证据」清单：

1. **编号阶段**：在 [src/njs_vm.h:22-82](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.h#L22-L82) 找出 `NJS_OBJ_TYPE_PROMISE` 的编号（应为 10）。
2. **声明阶段**：确认 `njs_object_type_init[10]` 指向 `&njs_promise_type_init`（[src/njs_builtin.c:57](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_builtin.c#L57)），并去 [src/njs_promise.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_promise.h) 确认它的 `constructor_props`/`prototype_props`/`prototype_value` 三件套分别来自哪张声明表。
3. **物化阶段**：在 `njs_builtin_objects_create` 的循环里（[src/njs_builtin.c:227-277](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_builtin.c#L227-L277)）指出当 `i == 10` 时，Promise 的构造器与原型分别被填进 `shared->constructors[10]`、`shared->prototypes[10]`。
4. **复用阶段**：说明任意克隆 VM 通过 `njs_vm_protos_init`（[src/njs_vm.c:576-609](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L576-L609)）拿到 `vm->constructors[10]` 副本，并由 `njs_vm_constructors_init` 把 `Promise.prototype.__proto__` 接到 `Object.prototype`（因为它落在普通类型段 `[OBJECT, NORMAL_MAX)` 内，见 [src/njs_vm.c:523-525](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L523-L525)）。
5. **访问阶段**：当 JS 执行 `Promise` 时，globalThis 的 `njs_top_level_constructor` 处理器（[src/njs_builtin.c:576-598](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_builtin.c#L576-L598)）用 `magic16 = NJS_OBJ_TYPE_PROMISE` 从 `njs_vm_ctor(vm, 10)` 取出构造器返回。

**产出**：一张标注了文件、行号、`i` 取值的流程图或编号清单。完成它，你就把本讲的「枚举 → 声明表 → shared 物化 → per-VM 副本 → 按需取用」五环彻底打通了。

## 6. 本讲小结

- `NJS_OBJ_TYPE_*` 是给「所有需要构造器+原型的内建类型」编号的枚举，**编号即数组下标**，按普通/隐藏/TypedArray/Error 四段连续排列，共 37 个类型 + 1 个 `MAX` 哨兵。
- njs 用「声明驱动物化」：先用 `NJS_DECLARE_PROP_*` 宏写静态声明表，再用 `njs_object_hash_create` 循环插进哈希。两张表 `njs_object_init[]`（5 个单例）和 `njs_object_type_init[]`（38 个类型，按下标）分别覆盖两类内建。
- `njs_builtin_objects_create` 只在模板 VM 跑一次，把 `njs_vm_shared_t` 装满：7 张实例方法哈希、5 个单例对象、38 个构造器、38 个原型。关键不变量是「`njs_vm_ctor_push` 的下标必须等于类型编号」。
- 「shared 层共享、per-VM 物化」：`shared` 只读、被所有克隆复用；每个 VM 用 `njs_vm_protos_init` 从 shared `memcpy` 出 `constructors/prototypes` 副本，再用 `njs_vm_constructors_init` 在副本上接原型链（`__proto__`）——因为原型链是可写状态，不能放进只读的 shared。
- globalThis 上的构造器是 `HANDLER` 属性，由 `njs_top_level_constructor` 用 `magic16`（即类型编号）从 `vm->constructors` **按需取用**，而非启动时全量塞进全局对象。

## 7. 下一步学习建议

- 本讲讲的是「njs 内置引擎」如何注册内建。如果你关心「另一个引擎 QuickJS」怎么做同样的事，可进入 u6（QuickJS 集成与双引擎架构），对比 `qjs_new_context` 里 `qjs_add_intrinsic_*` 的裁剪/补充方式与本节的声明表有何不同。
- 本讲多次提到「构造器是 `njs_function_t`、原生函数靠 `njs_function_native_t`」，但没展开函数对象本身的结构——这正是 u5-l4「外部对象与原生函数」的主题，建议接着读。
- 想验证本讲的运行时效果，可结合 u10-l4 的调试技巧：用 `./build/njs -d` 反汇编 `class extends` 之类代码，观察它如何通过 `NJS_OBJ_TYPE_*` 编号去定位父类原型，从而加深「编号即下标」的直觉。
