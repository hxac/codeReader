# 对象模型：共享哈希、本地哈希与原型链

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `njs_object_t` 的内存布局，特别是它**同时持有两张哈希表**（私有 `hash` + 共享 `shared_hash`）的原因。
- 完整描述一次属性读取（`obj.foo`）在内核里走过的调用链：`njs_value_property → njs_property_query → njs_object_property_query`，并解释它如何沿 `__proto__` 原型链逐层回溯。
- 解释**写时复制**（copy-on-write）：当你给一个继承自原型的属性赋值时，引擎如何把只读的「共享属性」惰性地物化为「实例私有副本」，而这个过程的核心函数就是 `njs_prop_private_copy`。

本讲只聚焦 njs 内置引擎（`src/`）下的对象模型，不涉及 QuickJS 侧（u6）和外部对象（u5-l4）。

## 2. 前置知识

在进入源码前，先用通俗语言铺垫几个概念。

- **对象与属性**：JavaScript 里「对象」本质上是一组「属性名 → 属性描述符」的映射。属性描述符不止包含值，还包含 `writable`（可写）、`enumerable`（可枚举）、`configurable`（可配置）三个特性位，或者一组 `get/set` 访问器。
- **原型链（prototype chain）**：每个对象内部有一个 `__proto__` 指针指向另一个对象（它的原型）。访问 `obj.foo` 时，若 `obj` 自身没有 `foo`，就顺着 `__proto__` 往上找，直到找到或到链尾（`null`）。这是 JS 实现「方法共享」的基础。
- **装箱（boxing）**：原始值（数字、字符串、布尔、符号）本身不是对象。当你写 `'abc'.length` 时，引擎会临时把它「装」进对应的包装对象（如 `String` 实例），借原型链查到 `length`，再丢弃包装对象。
- **atom_id**：njs 把所有属性名/标识符/符号「驻留」成一个 32 位整数（参见 u2-l4）。所以属性查找实际上是「整数 → 整数」的哈希查找，不需要逐字符比较字符串。
- **flathsh 扁平哈希表**：njs 自研的哈希表（参见 u2-l3），`njs_flathsh_unique_find` 直接用 `key_hash`（即 atom_id）做匹配，是 O(1) 的整数查找。
- **WHITEOUT**：njs 内部的一种属性类型标记，表示「这个属性曾经存在但已被 `delete`」。它的作用类似「墓碑（tombstone）」，让查找能区分「不存在」和「已删除」。

如果你对 atom_id、flathsh、四级存储（`vm->levels`）还不熟悉，建议先看 u2-l4 与 u4-l2。本讲的「属性」概念与 u4-l2 的「变量槽位」是两套不同机制：变量槽位服务于字节码操作数寻址，而本讲的对象属性哈希表服务于动态的 `obj[key]` 访问。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/njs_value.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h) | 定义 `njs_object_t`、`njs_object_prop_t`、`njs_property_query_t` 等核心结构，以及一堆 `njs_is_*`/`njs_prop_value` 宏。 |
| [src/njs_object.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_object.h) | 对象属性的描述符标志位（`njs_object_prop_flags_t`）、`njs_prop_private_copy` 等函数声明。 |
| [src/njs_object.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_object.c) | `njs_object_alloc`（分配对象）、`njs_object_value_copy`（深拷贝）、`njs_object_hash_create`、`__proto__`/`constructor` 的惰性 getter。 |
| [src/njs_object_prop.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_object_prop.c) | `njs_object_property`、`njs_prop_private_copy`（写时复制的核心）、`njs_object_prop_define`（`[[DefineOwnProperty]]`）。 |
| [src/njs_value.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.c) | 属性查找链的入口 `njs_value_property`、`njs_property_query`、`njs_object_property_query`，以及 `njs_value_property_set`。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. 对象结构与双哈希——对象「长什么样」，为什么有两张哈希表。
2. 属性查找链——`obj.foo` 如何一层层找到 `foo`。
3. 写时复制提升——给继承属性赋值时，共享属性如何变成私有副本。

### 4.1 对象结构与双哈希

#### 4.1.1 概念说明

在 njs 内部，所有「JS 对象」（普通对象、数组、函数、正则、日期、Promise、TypedArray 等等）的内存首部都嵌入了一个 `njs_object_t`。以普通对象为例，它的核心结构是：

```c
struct njs_object_s {
    njs_flathsh_t   hash;          // 私有、可变的属性哈希表（own properties）
    njs_flathsh_t   shared_hash;   // 共享、只读的属性哈希表（来自原型模板）
    njs_object_t   *__proto__;     // 原型指针，串成原型链
    njs_exotic_slots_t *slots;     // 外部对象（NGINX 的 r/s）的扩展槽，普通对象为 NULL
    njs_value_type_t type:8;
    uint8_t   shared;              // 该对象是否处于「模板共享」状态
    uint8_t   extensible:1;        // 是否可扩展（Object.preventExtensions 后为 0）
    uint8_t   error_data:1;
    uint8_t   stack_attached:1;
    uint8_t   fast_array:1;        // 数组是否走「连续内存快路径」
};
```

定义见 [src/njs_value.h:L158-L176](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L158-L176)。

**为什么要同时有 `hash` 和 `shared_hash` 两张表？** 这是本讲最关键的洞察，动机是**性能与内存占用（footprint）的平衡**：

- `Object.prototype.toString`、`Array.prototype.push` 这类「方法」对成千上万个实例来说都是**完全一样**的。如果每个对象都各自存一份，内存会被海量重复的属性条目撑爆。
- njs 的做法是：把这些「对所有实例都一样、且通常不会被改写」的属性放进一张**只读共享表** `shared_hash`。这张表的内容来自引擎启动时构建的模板（参见 u5-l3 内建对象注册），可以被大量对象以指针/浅拷贝的方式**复用**，几乎不占额外内存。
- 而那些「实例自己写入、彼此不同」的属性，才放进**私有可变表** `hash`。每个对象初始时 `hash` 是空的，只有在程序真正给对象赋值时才会增长。

这和 V8 用「隐藏类（hidden class）+ 共享原型」来压缩对象内存的思路一脉相承，只是 njs 用了两张显式的 flathsh 来表达。

`shared` 这个 1 位标志则标记「对象本身是不是模板里那个被多处共享的原始对象」。一旦某个对象被实际使用并需要修改，`njs_object_value_copy` 会先把它拷贝一份、把 `shared` 清零，再修改副本——这就是后面要讲的写时复制思想。

#### 4.1.2 核心流程

新对象通过 `njs_object_alloc` 创建，它做了三件典型初始化：

```c
njs_flathsh_init(&object->hash);           // 私有表初始化为空
njs_flathsh_init(&object->shared_hash);    // 共享表初始化为空（普通对象）
object->__proto__ = njs_vm_proto(vm, NJS_OBJ_TYPE_OBJECT);  // 原型指向 Object.prototype
object->shared = 0;        // 新分配的对象本身是私有的
object->extensible = 1;    // 默认可扩展
```

见 [src/njs_object.c:L39-L63](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_object.c#L39-L63)。注意：用 `new Object()` / 字面量 `{}` 创建的普通对象，`shared_hash` 是**空的**——它的「共享方法」是经由 `__proto__` 指向 `Object.prototype` 才拿到的。真正「装满共享属性」的是那些**原型对象**和**内建构造器**，它们通过 `njs_object_hash_create` 批量灌入属性：

```c
// 把预定义属性数组 prop[0..n) 逐条插入 hash
fhq.key_hash = prop->desc.atom_id;
njs_flathsh_unique_insert(hash, &fhq);
obj_prop->type = prop->desc.type;
obj_prop->enumerable = prop->desc.enumerable;
...
```

见 [src/njs_object.c:L158-L193](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_object.c#L158-L193)。

两张表用的是同一套 flathsh 协议（哈希算法、键语义相同），定义在 `njs_object_hash_proto`：

```c
const njs_flathsh_proto_t njs_object_hash_proto njs_aligned(64) = {
    NULL,                    // test 回调为 NULL → 直接用 key_hash 比较（即 atom_id）
    njs_flathsh_proto_alloc,
    njs_flathsh_proto_free,
};
```

见 [src/njs_object.c:L196-L202](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_object.c#L196-L202)。`test` 回调为 `NULL` 正是 u2-l4 讲过的 `unique_find`/`unique_insert`——只比 `key_hash`（atom_id）即可命中，无需字符串比较。

最后看一个属性的「真身」——`njs_object_prop_t`。它是 flathsh 的元素，同时也是一个完整的属性描述符：

```c
struct njs_object_prop_s {
    uint32_t  next_elt:26;   // flathsh 拉链用的「下一个元素」下标
    uint32_t  type:3;        // NJS_PROPERTY / NJS_ACCESSOR / NJS_PROPERTY_HANDLER / NJS_WHITEOUT ...
    uint32_t  writable:1;
    uint32_t  enumerable:1;
    uint32_t  configurable:1;
    uint32_t  atom_id;       // 属性名（驻留后的整数）
    union {
        njs_value_t  *val;
        njs_value_t   value;     // 数据属性的值（njs_prop_value 取这个）
        struct { njs_function_t *getter, *setter; } accessor;  // 访问器属性
        ...
    } u;
};
```

见 [src/njs_value.h:L311-L344](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L311-L344)，类型枚举见 [src/njs_value.h:L289-L299](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L289-L299)。注意它刻意把 `next_elt`（链表下标）和「属性描述符」塞进同一个 32 位字，使得 flathsh 元素和属性描述符共用同一块内存——这是「扁平哈希」省指针的关键（u2-l3 讲过 flathsh 用数组下标而非指针串联拉链）。

#### 4.1.3 源码精读

- 对象分配与字段初值：[src/njs_object.c:L39-L63](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_object.c#L39-L63) —— `hash`/`shared_hash` 清空、`__proto__` 指向 `Object.prototype`、`shared=0`/`extensible=1`。
- 对象哈希协议：[src/njs_object.c:L196-L202](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_object.c#L196-L202) —— `test=NULL`，纯按 atom_id 查找。
- 批量灌入预定义属性：[src/njs_object.c:L158-L193](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_object.c#L158-L193) —— `njs_object_hash_create`。
- `njs_object_t` 结构定义：[src/njs_value.h:L158-L176](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L158-L176)。
- 属性描述符结构与访问宏：[src/njs_value.h:L311-L344](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L311-L344)。

#### 4.1.4 代码实践

**目标**：直观感受「实例对象 `hash` 为空，方法都来自原型链上的 `shared_hash`」。

**步骤**：

1. 按上一讲（u1-l3）构建 CLI：`./configure && make njs`，得到 `build/njs`。
2. 运行下面这段（待本地验证）：

```bash
./build/njs -c '
var o = {};                         // 普通对象，hash 为空
console.log(typeof o.toString);     // "function"：来自 Object.prototype
console.log(o.hasOwnProperty("x")); // false：o 自身没有 x
o.x = 1;                            // 现在 o.hash 里才有了一个 x
console.log(o.hasOwnProperty("x")); // true
console.log(({}).hasOwnProperty("x")); // 仍为 false，因为是另一个对象
'
```

**需要观察的现象**：`o` 从未定义 `toString`，却总能调用它——因为查找会沿 `__proto__` 走到 `Object.prototype` 的 `shared_hash`。

**预期结果**：依次输出 `function`、`false`、`true`、`false`。最后一条特别重要：它说明「给 `o.x` 赋值」只改了 `o` 自己的 `hash`，绝没有去碰 `Object.prototype`，否则 `({}).x` 也会变成 1。

#### 4.1.5 小练习与答案

**练习 1**：`njs_object_t` 里的 `shared_hash` 字段为什么不直接复用一个全局静态表，而要存在每个对象内部？

**参考答案**：`shared_hash` 虽然内容是「共享只读」的，但它仍是一个 `njs_flathsh_t`（含描述符指针、元素数组等），不同类型的对象需要不同的共享属性集合（`Array.prototype` 的方法集 ≠ `Object.prototype` 的方法集）。把 `shared_hash` 放在每个对象内，引擎就能让「原型对象」携带本类型专属的共享属性，而普通实例只需让 `__proto__` 指向它即可。

**练习 2**：`njs_object_hash_proto` 的 `test` 回调为什么可以是 `NULL`？

**参考答案**：因为属性键已经被驻留成 32 位 atom_id，并直接存进 flathsh 的 `key_hash` 字段。`njs_flathsh_unique_find` 只比较 `key_hash` 两个整数是否相等，无需再调用 `test` 做字符串内容比对（参见 u2-l3、u2-l4）。

### 4.2 属性查找链

#### 4.2.1 概念说明

当 JS 里写 `obj.foo` 时，引擎要找到 `foo` 对应的属性描述符。njs 把这件事拆成三层调用，由外到内分别是：

```
njs_value_property          顶层入口：先走数组/TypedArray 快路径，否则进入慢路径
  └─ njs_property_query     按 value 的类型分派：原始值装箱、取原型对象、特殊类型处理
       └─ njs_object_property_query   真正的原型链遍历循环（本节主角）
```

- **`njs_value_property`**：负责 GET 的快速情形。如果是数字下标（`atom_id` 最高位为 1 的 number atom），且对象是快数组或 TypedArray，直接按下标读写连续内存，根本不查哈希表——这是热路径优化。只有「慢路径」（命名属性、或非快数组）才调 `njs_property_query`。
- **`njs_property_query`**：根据 `value->type` 决定从哪个对象开始查。例如对原始值 `NJS_NUMBER`，它会「装箱」——把 `value` 映射到 `Number.prototype` 这个原型对象再查；对真正的对象类型，直接取 `njs_object(value)`。
- **`njs_object_property_query`**：核心循环，沿 `__proto__` 链逐层查 `hash` 与 `shared_hash`。

这套分层让「数组下标访问」和「普通属性访问」走完全不同的代码路径，互不拖累。

#### 4.2.2 核心流程

`njs_object_property_query` 的查找循环可以浓缩成下面这段伪代码（已省略数组/TypedArray/字符串实例的特殊下标处理）：

```
own = pq->own          # 记住调用方是否只关心「自身属性」（hasOwnProperty）
pq->own = 1            # 第一层（对象自身）总是当作 own
proto = object

do:
    # 1. 先查当前层的私有表 hash
    if unique_find(proto->hash, atom_id) == OK:
        prop = 命中的属性
        if prop.type != WHITEOUT:
            return OK              # 找到真正的自身属性
        否则记录 own_whiteout = &proto->hash   # 这是个删除标记
    # 2. 私有表没有，再查共享表 shared_hash
    else if unique_find(proto->shared_hash, atom_id) == OK:
        return njs_prop_private_copy(pq, proto)   # 见 4.3 节
    # 3. 若只查自身属性，到此为止
    if own:
        return DECLINED
    # 4. 否则上溯原型链
    pq->own = 0
    proto = proto->__proto__
while proto != NULL

return DECLINED                      # 整条链都没找到
```

对应源码 [src/njs_value.c:L669-L792](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.c#L669-L792)。

这条查找链的**平均时间复杂度**可以写成：

\[
T_{\text{lookup}} = O\big(d \cdot (T_{\text{hash}} + T_{\text{shared}})\big)
\]

其中 \(d\) 是原型链长度，\(T_{\text{hash}}\)、\(T_{\text{shared}}\) 都是 `unique_find` 的整数哈希查找，均摊 \(O(1)\)。所以一次属性访问在平均情况下是 \(O(d)\)，与原型链深度成正比——这也解释了为什么过长的原型链（或 `__proto__` 链回环）会影响性能。

有几个细节值得记住：

- **`own` 标志**：调用 `hasOwnProperty` 时 `pq->own=1`，循环第一层查完就返回 `DECLINED`（不沿原型链）。普通属性访问 `pq->own=0`，会一直走到链尾。
- **WHITEOUT 墓碑**：私有表里命中一个 `NJS_WHITEOUT` 类型的条目，意味着「这个属性曾被 `delete`」。代码不会返回它，而是记下它的位置 `pq->own_whiteout`（供后续 SET 复用槽位），然后继续往下查。
- **shared_hash 命中即触发写时复制**：注意 `else if` 分支——一旦在 `shared_hash` 找到，立刻 `return njs_prop_private_copy(...)`，把这条共享属性「物化」进当前层的私有表后再返回（详见 4.3）。
- **外部对象兜底**：回到 `njs_property_query`，如果普通查找返回 `DECLINED` 且对象带 `slots`（即 NGINX 注入的外部对象如 `r`/`s`），会改走 `njs_external_property_query`——那是 u5-l4 的主题，这里只做了解。

#### 4.2.3 源码精读

- 顶层 GET 入口（快/慢路径分派）：[src/njs_value.c:L1048-L1167](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.c#L1048-L1167) —— `njs_value_property`。命中后按 `prop->type` 分三种取值方式：数据描述符直接取 `njs_prop_value`、访问器调用 getter、`NJS_PROPERTY_HANDLER` 调用 prop_handler。
- 按类型分派与装箱：[src/njs_value.c:L610-L666](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.c#L610-L666) —— `njs_property_query`。`NJS_BOOLEAN/NJS_NUMBER/NJS_SYMBOL` 通过 `njs_primitive_prototype_index` 装箱到对应原型。
- 原型链遍历核心循环：[src/njs_value.c:L669-L792](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.c#L669-L792) —— `njs_object_property_query`。
- 一个更精简的查找版本（不带 own/SET 语义，专给「取属性值」用）：`njs_object_property`，见 [src/njs_object_prop.c:L45-L95](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_object_prop.c#L45-L95)。它的循环结构同样是「`hash` → `shared_hash` → `__proto__`」，是理解查找逻辑的好入口。
- 查询上下文结构 `njs_property_query_t`：[src/njs_value.h:L352-L364](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L352-L364) —— 内含 `query`（GET/SET/DELETE）、`own`、`own_whiteout`、`scratch`（存放 PROPERTY_HANDLER 的求值结果）。

#### 4.2.4 代码实践

**目标**：跟踪一次「沿原型链查找」的属性访问，验证它确实穿越了多层。

**步骤**：

1. 构造一条长度为 3 的原型链，让属性定义在最顶层：

```bash
./build/njs -c '
var grand = { secret: 42 };
var parent = Object.create(grand);
var child  = Object.create(parent);
console.log(child.secret);        // 42：要穿越 child → parent → grand 才找到
console.log(child.__proto__ === parent);
console.log(child.__proto__.__proto__ === grand);
'
```

**需要观察的现象**：`child.secret` 输出 42，说明查找链确实沿 `__proto__` 一路走到底。`__proto__` 的两次比较输出 `true`，印证了原型链的结构。

**预期结果**：`42`、`true`、`true`。（待本地验证。）

2. **源码跟踪型实践**：打开 [src/njs_value.c:L669-L792](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.c#L669-L792)，对照上面的伪代码，为 `child.secret` 的访问标注出循环体在三次迭代中分别查了哪个对象的 `hash` 和 `shared_hash`、`proto` 指针如何变化、最终在哪一层命中。预期结论是：前两次（`child`、`parent`）两层表都 `DECLINED`，第三次（`grand`）在 `grand->hash` 命中。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `njs_value_property` 在调用 `njs_property_query` 之前，要先单独处理「数字下标 + 快数组 / TypedArray」的情形？

**参考答案**：因为数组元素访问是最高频的操作，而快数组把元素存在一段连续内存（`array->start[index]`）里，按下标直接读写远比走哈希表快。把这条「快路径」单独前置，可以让绝大多数数组访问绕开哈希查找与原型链遍历；只有越界、命名属性或慢数组才回退到通用的 `njs_property_query` 慢路径。

**练习 2**：`njs_object_property_query` 里，私有表 `hash` 命中一个 `NJS_WHITEOUT` 条目时，为什么不直接返回「未找到」？

**参考答案**：WHITEOUT 表示该属性「曾被 `delete`」。如果直接当未找到，后续 `obj.x = ...` 重新赋值时会在共享表里再次命中并触发一次不必要的复制；记下 `pq->own_whiteout` 的位置，可以让 SET 路径（`njs_object_prop_define`）复用这个墓碑槽位，把新属性直接写回原位置，避免哈希表反复扩缩。

### 4.3 写时复制提升

#### 4.3.1 概念说明

`shared_hash` 是「只读共享」的。但 JS 语义允许你对任何继承来的属性赋值，例如：

```js
var o = {};
o.toString = 123;   // 给「继承自 Object.prototype 的 toString」赋一个自己的值
```

按 ECMAScript 规范，这应该在 `o` **自身**创建一个新的 `toString` 数据属性（这叫「属性遮蔽 / shadowing」），而**不能**去改 `Object.prototype.toString`——否则所有对象的 `toString` 都会被改坏。

njs 用**惰性物化（lazy materialization）**解决这个问题：

- 平时，`toString` 只存在于 `Object.prototype` 的 `shared_hash` 里，成千上万个实例共享一份。
- 当某个实例**第一次需要写**（或第一次在 SET 路径上被查到）时，引擎把这条共享属性**复制一份到当前对象的私有 `hash`**，从此对这个实例而言，它就有了一份可写的私有副本。

承担这个「共享 → 私有」复制工作的就是 `njs_prop_private_copy`。这种「读时不复制、写时才复制」的策略就是经典的**写时复制（copy-on-write, COW）**，能大幅减少启动时一次性物化所有内建属性的开销和内存。

#### 4.3.2 核心流程

`njs_prop_private_copy(vm, pq, proto)` 的执行过程（`proto` 是命中 shared_hash 的那一层对象）：

```
shared = pq->fhq.value              # 共享表里命中的属性描述符
在 proto->hash 插入一个新条目 (unique_insert)
prop = 新插入的私有条目
# 1. 浅拷贝描述符字段
prop.enumerable/configurable/writable/type = shared 的对应字段
prop.u.value = shared.u.value

# 2. 若是访问器属性：深拷贝 getter/setter 函数
if 是 accessor:
    prop.getter = njs_function_copy(shared.getter)
    prop.setter = njs_function_copy(shared.setter)   # 可能复用 getter 副本
    return

# 3. 若是数据属性，且值是「可变容器」类型：深拷贝容器
value = prop.u.value
switch value.type:
    OBJECT / ARRAY / OBJECT_VALUE:  value.object = njs_object_value_copy(value)
    FUNCTION:                       value.function = njs_function_value_copy(value)
                                    并给函数补上正确的 name 属性
```

关键点：**深拷贝是必要的**。共享属性的值如果直接是某个对象/函数（比如某个内建 getter 闭包、某个内建对象常量），多个实例若共用同一个指针再各自修改，就会互相串值。因此当值是 `NJS_OBJECT/NJS_ARRAY/NJS_OBJECT_VALUE/NJS_FUNCTION` 时，必须递归地把这个值也「物化」成私有副本（`njs_object_value_copy` 内部还会先检查 `object->shared` 标志，只有共享对象才真正拷贝）。

#### 4.3.3 源码精读

- 写时复制主体：`njs_prop_private_copy`，[src/njs_object_prop.c:L565-L655](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_object_prop.c#L565-L655)。

  - 先在 `proto->hash` 插入条目并拷贝描述符字段：[src/njs_object_prop.c:L577-L591](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_object_prop.c#L577-L591)。
  - 访问器属性的 getter/setter 深拷贝（含「getter 与 setter 指向同一原生函数时复用副本」的小优化）：[src/njs_object_prop.c:L593-L621](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_object_prop.c#L593-L621)。
  - 数据属性的值按类型深拷贝：[src/njs_object_prop.c:L623-L654](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_object_prop.c#L623-L654)。

- 触发点：在查找循环里，`shared_hash` 命中即调用，[src/njs_value.c:L775-L780](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.c#L775-L780)。
- 容器对象的深拷贝与 `shared` 标志检查：`njs_object_value_copy`，[src/njs_object.c:L66-L117](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_object.c#L66-L117)。它先看 `if (!object->shared) return object;`——已是私有的对象直接返回，避免无谓拷贝。

#### 4.3.4 代码实践（本讲核心实践）

**目标**：结合 `njs_prop_private_copy` 的源码，完整描述「给一个继承自原型的属性赋值」时，从「共享属性」到「本地副本」的物化过程。

**步骤**：

1. **定位代码**。打开 [src/njs_object_prop.c:L565-L655](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_object_prop.c#L565-L655)（`njs_prop_private_copy`），再打开它的唯一调用点 [src/njs_value.c:L775-L780](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.c#L775-L780)。

2. **运行下面的最小用例**（待本地验证），观察赋值前后「自身属性」的变化：

```bash
./build/njs -c '
var proto = { greet: function () { return "hi"; } };
var o = Object.create(proto);
console.log(o.hasOwnProperty("greet"));   // false：greet 还只在原型上
var fn = o.greet;                          // 读取：触发 shared_hash 查找与物化
console.log(o.hasOwnProperty("greet"));    // 仍可能 false：物化进的是 proto.hash，不是 o.hash
console.log(fn());                          // hi
o.greet = function () { return "hello"; }; // 真正写 o 自身
console.log(o.hasOwnProperty("greet"));    // true：现在 o.hash 里有了
console.log(proto.greet());                // hi：原型未被改坏
'
```

3. **对照源码描述过程**。按以下顺序填空（这是你要交出的「完整过程」）：

   - 触发条件：查找循环在某一层 `proto->hash` 未命中，转而在 `proto->shared_hash` 命中 → 调用 `njs_prop_private_copy(vm, pq, proto)`。
   - 第一步（[L577-L584](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_object_prop.c#L577-L584)）：用 `njs_flathsh_unique_insert` 在该层 `proto->hash` 里**新建**一个空条目。
   - 第二步（[L586-L591](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_object_prop.c#L586-L591)）：把共享条目的 `enumerable/configurable/writable/type/u.value` **逐字段复制**到新条目。
   - 第三步（[L593-L654](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_object_prop.c#L593-L654)）：若值是访问器或对象/函数容器，递归深拷贝，避免共享可变状态。
   - 第四步：函数返回后，调用方拿到的是指向**私有副本**的 `pq->fhq.value`，后续对它的写操作只会影响这一层对象，绝不会污染共享模板。

**需要观察的现象**：`o.greet = ...` 之后 `o.hasOwnProperty("greet")` 变为 `true`，而 `proto.greet()` 依然返回 `hi`——这正是「写自身、不动原型」的语义，也印证了写时复制只物化到恰当的层。

**预期结果**：`false`、（取决于物化层）`false`、`hi`、`true`、`hi`。第二条 `hasOwnProperty` 的值是本实践的思考点：读取继承属性触发的物化写进了**原型层** `proto->hash` 而非 `o->hash`，所以 `o.hasOwnProperty("greet")` 仍可能是 `false`；只有 `o.greet = ...` 这种显式赋值才会在 `o` 自身创建属性。如果与你本地观察到的结果不一致，请以本地实际输出为准并据此修正理解。

#### 4.3.5 小练习与答案

**练习 1**：`njs_prop_private_copy` 为什么要对 `NJS_FUNCTION` 类型的值调用 `njs_function_value_copy` 并随后 `njs_function_name_set`？

**参考答案**：因为函数是可变对象（可能带 `prototype`、绑定参数等状态），如果多个实例共享同一个函数指针再各自修改，会互相串值，必须深拷贝成私有副本。拷贝后还要根据属性的 atom_id（即属性名字符串）重新设置函数的 `name` 属性，保证 `obj.someMethod.name === "someMethod"` 的语义正确。

**练习 2**：如果 `njs_object_value_copy` 不检查 `object->shared` 标志、对每个对象都无脑拷贝，会有什么后果？

**参考答案**：会导致大量无意义的拷贝——许多属性值其实已经是私有对象（`shared=0`），完全可以直接共用指针。无脑拷贝既浪费内存，又可能因为复制了本不该复制的可变状态而改变程序语义。检查 `shared` 标志确保「只对模板共享对象做一次物化」，这正是写时复制「惰性」的体现。

## 5. 综合实践

把三个模块串起来，完成下面这个「对象属性全链路观察」任务。

**任务**：解释下面这段 JS 在 njs 内核里的完整执行轨迹（**源码阅读型实践**，不必修改内核）：

```js
var base = { type: "base" };                 // (A)
var obj = Object.create(base);               // (B)
obj.id = 7;                                   // (C) 写自身属性
console.log(obj.type);                        // (D) 读继承属性
console.log(obj.id);                          // (E) 读自身属性
delete obj.id;                                // (F) 删除自身属性
console.log(obj.id);                          // (G) 再次读，会怎样？
```

**要求**：对标注 (A)~(G) 的每一行，用本讲学过的概念说明：

- 它动了哪个对象的 `hash` / `shared_hash`？
- 走的是快路径还是 `njs_property_query` 慢路径？
- 是否触发了 `njs_prop_private_copy`？触发了的话，物化到了哪一层？
- (F) 删除后，`obj->hash` 里 `id` 对应的条目变成了什么类型？（提示：回忆 WHITEOUT。）

**参考分析要点**（请先自己思考再对照）：

- (A) `base` 是普通对象，`base->hash` 得到一个 `type` 条目，`shared_hash` 为空。
- (B) `obj.__proto__ = base`，`obj->hash`、`obj->shared_hash` 均空。
- (C) 走 SET 慢路径，查找链在 `obj->hash` 未命中、在共享表也未命中 → 最终在 `obj->hash` 新建 `id` 条目（参见 `njs_object_prop_define` 的 `set_prop` 分支）。
- (D) 走 GET 慢路径：`obj->hash` 未命中、`obj->shared_hash` 未命中，上溯到 `base`，在 `base->hash` 命中 `type`。
- (E) `obj->hash` 直接命中 `id`。
- (F) `delete` 把 `obj->hash` 里的 `id` 条目改写成 `NJS_WHITEOUT`（而非真正删除节点），这正是 4.2 节提到的「墓碑」。
- (G) `obj->hash` 命中 WHITEOUT → 视为未找到 → 上溯 `base`，`base` 也没有 `id` → 返回 `undefined`。

可以配合 `./build/njs -c '...'` 实际运行，验证你对 (D)(E)(G) 输出的预测（预期：`base`、`7`、`undefined`）。

## 6. 本讲小结

- `njs_object_t` 用**两张 flathsh**表达属性：只读共享的 `shared_hash`（压缩内建方法的内存占用）+ 实例私有可变的 `hash`（存 own properties）；外加 `__proto__` 串成原型链。
- 属性访问分三层：`njs_value_property`（快/慢路径分派）→ `njs_property_query`（按类型装箱、取原型对象）→ `njs_object_property_query`（沿 `__proto__` 逐层查 `hash` 再查 `shared_hash`，平均 \(O(d)\)）。
- 查找用 atom_id 作为 `key_hash` 做 `unique_find`，纯整数匹配；`own` 标志控制是否只查自身；`NJS_WHITEOUT` 是 `delete` 留下的墓碑。
- **写时复制**是核心优化：`shared_hash` 命中时由 `njs_prop_private_copy` 把共享属性惰性物化进当前层的私有 `hash`，并对函数/对象值深拷贝，保证「写自身、不污染共享模板」。
- 数组下标访问走快路径（连续内存），不进哈希表，是热路径优化。

## 7. 下一步学习建议

- 下一讲 **u5-l2（原始内建类型：string / array / number）** 会进入 `njs_string.c` / `njs_array.c` / `njs_number.c`，讲解字符串与数组的内部表示，以及快/慢数组的转换路径——其中数组元素的查找与本讲的 `njs_array_property_query` 衔接，值得对照阅读。
- 若想深入「内建对象如何在启动时灌进 `shared_hash`」，可先看 **u5-l3（内建对象与构造器的注册）**，它讲解 `njs_object_hash_create` 与 `njs_vm_shared_t` 的协作。
- 若想了解外部对象（NGINX 的 `r`/`s`）如何复用这套属性机制（通过 `slots` 与 `njs_external_property_query`），请看 **u5-l4（外部对象与原生函数）**。
- 建议同步复习 **u2-l3（内存池与 flathsh）** 与 **u2-l4（Atom 表）**，本讲大量依赖这两讲建立的 `unique_find`/atom_id 心智模型。
