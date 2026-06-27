# Atom 表：字符串与符号的驻留

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚为什么 njs 要把每一个属性名、标识符、已知符号「驻留（intern）」成一个 32 位的 `atom_id`，而不是到处拷贝字符串。
- 解释 number atom 的位编码：最高位 `0x80000000` 当作标志位，把一个非负整数直接「打包」进 `atom_id`，省去一次哈希表查找。
- 区分模板 VM 的共享 atom 表 `atom_hash_shared` 与每个克隆 VM 的私有 atom 表 `atom_hash`，并说明克隆时 `shared_atom_count` 这条分界线的作用。
- 在 `src/njs_atom_defs.h` 里快速定位 `length`、`prototype`、`Symbol.iterator` 等常用原子对应的 `NJS_ATOM_*` 常量。

本讲是 u2-l2（16 字节值表示）和 u2-l3（内存池与 flathsh 哈希表）的直接延续：`atom_id` 正是写在 `njs_value_t` 前 4 字节里的那个字段，而所有 atom 又都被装进 u2-l3 讲过的 `njs_flathsh_t` 哈希表里。

## 2. 前置知识

在进入源码前，先用通俗语言建立两个直觉。

**第一个直觉：为什么要「原子化」？**

JavaScript 程序里到处都是字符串当键：`obj.length`、`arr["prototype"]`、`Symbol.iterator`。如果每一次属性访问都要把键字符串重新分配、重新比较，开销会非常大。常见的优化思路是**驻留（interning）**：把每个出现过的字符串键只存一份，给它发一个唯一的小整数编号。之后引擎内部传递、比较的只是这个编号，而不再是字符串本体。njs 把这个编号叫做 **atom_id**，是一个 32 位无符号整数。

这样一来：

- 比较「两个键是否相同」从「逐字节比较字符串」退化成「比较一个 32 位整数」。
- 字节码里存属性名只需存一个 32 位 id，而不是变长字符串。
- `njs_value_t` 的前 4 字节（u2-l2 讲过的 `atom_id`/`magic32` 双关字段）就可以直接携带这个键。

**第二个直觉：一个 32 位整数怎么既当「名字」又当「数字」？**

属性键不仅可以是字符串/符号，还可以是数字下标，比如 `arr[3]`、`obj[0]`。如果对每一个数字都新建一个哈希表条目，太浪费。njs 的做法是**位编码**：用一个标志位区分两种含义。

- 当最高位 `0x80000000` **置 1** 时，这个 `atom_id` 是一个 **number atom**：剩下的低 31 位就是那个整数本身（比如 `3` 编码成 `0x80000003`）。
- 当最高位 **为 0** 时，这个 `atom_id` 是一个 **named atom**：整个 32 位是一个索引，指向共享表或私有表里某一条字符串/符号。

这两种编码互不冲突：number atom 永远 ≥ `0x80000000`，named atom 永远 < `0x80000000`。一个位运算就能判别。

> ⚠️ 注意区分两套「最高位」用法。本讲后面会看到 `njs_atom_string_key()` 和 `njs_atom_symbol_key()` 也用了 `0x80000000`，但那是给 **flathsh 的 key_hash**（哈希值）用的，用来把字符串 atom 和符号 atom 分到不同的冲突空间；它和 `atom_id` 上的 number-atom 标志位是**两件不同的事**，只是恰好都借用了最高位。读源码时不要混淆。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|---|---|
| [src/njs_atom_defs.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_atom_defs.h) | 一张「清单文件」：用 `NJS_DEF_STRING` / `NJS_DEF_SYMBOL` 宏逐条声明所有预定义原子（字符串 + 符号）。它是唯一的数据来源，被多处 `#include` 后展开成不同形态。 |
| [src/njs_atom.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_atom.h) | 把 `njs_atom_defs.h` 展开成枚举 `NJS_ATOM_STRING_*` / `NJS_ATOM_SYMBOL_*`，并提供内联函数 `njs_atom_to_value`（由 atom_id 反查 value）。 |
| [src/njs_atom.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_atom.c) | atom 表的实现：静态值数组 `njs_atom[]`、共享表装填 `njs_atom_hash_init`、查找 `njs_atom_find`、新增 `njs_atom_add` / `njs_atom_symbol_add`、把任意键原子化的 `njs_atom_atomize_key`。 |
| [src/njs.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs.h) | 公共头里定义了三个位运算宏 `njs_atom_is_number` / `njs_atom_number` / `njs_number_atom`。 |
| [src/njs_vm.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.h) | `njs_vm_s` 结构里关于 atom 表的字段（`atom_hash_shared` / `atom_hash` / `atom_hash_current` / `shared_atom_count` / `atom_id_generator`）。 |

## 4. 核心概念与源码讲解

### 4.1 atom 驻留与位编码

#### 4.1.1 概念说明

「atom」就是「被驻留的字符串/符号」的身份证号。njs 在两个层面用到 atom：

1. **键层面**：访问对象属性 `obj.foo` 时，键 `foo` 是一个 atom。属性查找的最终入口 `njs_property_query` 接收的不是字符串，而是一个 `atom_id`（见 4.1.3）。
2. **值层面**：u2-l2 讲过，每个 `njs_value_t` 的前 4 字节都带一个 `atom_id` 字段。当一个 value 恰好充当属性键时，这个字段就是它的 atom 身份证。

number atom 是这套机制里最巧妙的一环。它利用了「属性键经常是连续小整数」这一现实：与其为 `arr[0]`、`arr[1]`、…、`arr[n]` 每个都建一条哈希表记录，不如直接把整数编码进 `atom_id`，需要时再用一个位运算还原。这把「数字键查找」从一次哈希操作降级为一次纯算术。

#### 4.1.2 核心流程

把一个键原子化的标准流程（`njs_atom_atomize_key`）：

```text
输入一个 atom_id == NJS_ATOM_STRING_unknown 的值 value
├─ 若 value 是 NJS_STRING
│   ├─ 尝试把它解释成整数下标（njs_key_to_index）
│   ├─ 若成功且 0 <= n < 0x80000000（且非 -0）
│   │     value.atom_id = njs_number_atom(n)     ← 直接打包成 number atom
│   └─ 否则
│         计算 djb 哈希 → 先 njs_atom_find → 命中则复用
│                       → 未命中则 njs_atom_add 新建一个 named atom
├─ 若 value 是 NJS_NUMBER
│   └─ 同上：能装下就 number atom，否则转成字符串再 atom 化
└─ 若 value 是 NJS_SYMBOL：atom_id 在创建时已分配，什么都不做
```

关键判别只有一个位运算：`atom_id & 0x80000000` 是否非零。

#### 4.1.3 源码精读

三个位运算宏定义在公共头里，是理解整套机制的钥匙：

[njs.h:68-70](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs.h#L68-L70) —— 测最高位、还原整数、打包整数：

```c
#define njs_atom_is_number(atom_id) ((atom_id) & 0x80000000)
#define njs_atom_number(atom_id)    ((atom_id) & 0x7FFFFFFF)
#define njs_number_atom(n)          ((n) | 0x80000000)
```

- `njs_atom_is_number`：保留最高位、清掉其余，结果非零即「是 number atom」。
- `njs_atom_number`：屏蔽最高位，把低 31 位还原成原始整数。
- `njs_number_atom`：反向操作，给整数或上最高位，打包成 `atom_id`。

接下来看 `njs_atom_atomize_key` 是怎么决定用哪种编码的。[njs_atom.c:235-261](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_atom.c#L235-L261) 处理 `NJS_STRING` 分支：

```c
case NJS_STRING:
    num = njs_key_to_index(value);
    u32 = (uint32_t) num;

    if (njs_fast_path(u32 == num && (u32 < 0x80000000)
                      && !(num == 0 && signbit(num))))
    {
        value->atom_id = njs_number_atom(u32);     // ← 数字键走 number atom
    } else {
        hash_id = njs_djb_hash(value->string.data->start,
                               value->string.data->size);
        entry = njs_atom_find(vm, value->string.data->start,
                              value->string.data->size, hash_id);
        if (entry == NULL) {
            entry = njs_atom_add(vm, value, hash_id);   // ← 字符串键走 named atom
            ...
        }
        *value = *entry;
    }
```

注意那个 `!(num == 0 && signbit(num))` 的判断：它专门排除 `-0`。因为 `-0` 转成 `uint32_t` 也是 `0`，会被错误地编码成 `njs_number_atom(0)`，丢失了负零的信息，所以要挡掉。

那么，一个 named atom 的 `atom_id` 是怎么发出来的？看 `njs_atom_add`：

[njs_atom.c:117-129](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_atom.c#L117-L129) —— 新原子的 id 由一个单调递增计数器分配：

```c
prop = fhq.value;
prop->u.value = *value;
prop->u.value.string.atom_id = vm->atom_id_generator++;   // ← 发号
if (njs_atom_is_number(prop->u.value.string.atom_id)) {    // ← 用完最高位就报错
    njs_internal_error(vm, "too many atoms");
    return NULL;
}
```

也就是说，运行期动态产生的 named atom，id 从 `atom_id_generator` 的当前值开始往上加；万一加到撞上 `0x80000000`（即 number atom 的领地），就认为「atom 太多了」直接报错——这是 number atom 位编码带来的天然上限。

最后看属性查找最末端如何消费这个 `atom_id`。[njs_value.h:1010-1017](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L1010-L1017) 表明，键被原子化后，`njs_property_query` 拿到的就只是一个 `atom_id`：

```c
        ret = njs_atom_atomize_key(vm, key);
        ...
    }

    return njs_property_query(vm, pq, value, key->atom_id);   // ← 只传 atom_id
```

这意味着属性查找的内层循环再也不碰字符串，只比较 32 位整数——这正是原子化的全部收益。

#### 4.1.4 代码实践

**实践目标**：亲手验证 number atom 的位编码，并理解它如何被属性查找直接使用。

**操作步骤**：

1. 打开 `src/njs.h` 第 68–70 行，确认三个宏的定义。
2. 在脑中（或用 `python3 -c`）计算几个例子：
   - `njs_number_atom(3)` = `3 | 0x80000000` = `0x80000003` = `2147483651`
   - `njs_atom_is_number(0x80000003)` = `0x80000003`（非零，判定为 number atom ✅）
   - `njs_atom_number(0x80000003)` = `0x80000003 & 0x7FFFFFFF` = `3`（还原成功 ✅）
3. 对照 `njs_value_property_i64`（[njs_value.h:1020-1033](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L1020-L1033)）：当 `index < 0x80000000` 时，它直接调 `njs_value_property(vm, value, njs_number_atom(index), ...)`，跳过了所有字符串/哈希处理。

**需要观察的现象**：

- 小整数下标走的是一条「纯算术」的快路径，没有任何哈希表参与。
- 当下标 ≥ `0x80000000`（约 21 亿）时，代码退回到 `njs_set_number` + `njs_value_property_val` 的慢路径——因为 31 位已经装不下这个数了。

**预期结果**：能口述出「`arr[3]` 的键最终以 `0x80000003` 这个 `atom_id` 进入 `njs_property_query`」。

### 4.2 共享 vs 私有 atom 哈希

#### 4.2.1 概念说明

u2-l1 讲过 njs 的克隆模型：先 `njs_vm_create` 建一个**模板 VM**（编译一次字节码），再 `njs_vm_clone` 给每个请求/session 复制一个**实例 VM**（隔离地执行）。这套模型要求**只读资源尽量共享、可变资源必须私有**。

atom 表正好是「绝大多数只读」的典型：

- 几百个预定义原子（`length`、`prototype`、`Symbol.iterator`、所有关键字……）在所有请求里都一模一样，理应只存一份。
- 但运行期动态产生的新 atom（比如 `eval` 出来的代码里的标识符）是每个请求私有的，不能污染别的请求。

于是 njs 设计了**两张表 + 一个指针**：

- `atom_hash_shared`：共享表，存放预定义原子，所有克隆只读复用。
- `atom_hash`：每个克隆私有的表，存放运行期新原子。
- `atom_hash_current`：一个指针，指向「当前写入/查找的目标表」。模板 VM 里它指向共享表，克隆 VM 里它指向自己的私有表。

`shared_atom_count` 则是一条「分界线」：所有 `< shared_atom_count` 的 atom_id 落在共享表里，其余的落在私有表里。

#### 4.2.2 核心流程

模板 VM 启动时：

```text
njs_builtin_objects_create (njs_builtin.c:150)
  └─ vm->atom_id_generator = njs_atom_hash_init(vm)
       ├─ 遍历静态数组 njs_atom[]，逐条插入 atom_hash_shared
       └─ atom_hash_current = &atom_hash_shared      ← 模板期：写共享表
     返回 NJS_ATOM_SIZE（= 预定义原子总数）
  ⇒ atom_id_generator = NJS_ATOM_SIZE                 ← 后续新 atom 从此编号
```

克隆时（`njs_vm_clone`）：

```text
nvm = *vm                         ← 浅拷贝：atom_hash_shared 随之被复用（共享！）
nvm->shared_atom_count = vm->atom_id_generator   ← 记下分界线
njs_flathsh_init(&nvm->atom_hash)                ← 私有表从空开始
nvm->atom_hash_current = &nvm->atom_hash         ← 此后写入走私有表
```

查找时（`njs_atom_find`）：先查当前表（私有），再查共享表。反向解码（`njs_atom_to_value`）则用 `shared_atom_count` 判断该去哪张表取值。

#### 4.2.3 源码精读

先看 VM 结构里这几个字段的位置。[njs_vm.h:131-135](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.h#L131-L135)：

```c
njs_flathsh_t            atom_hash_shared;   // 共享表（值，随浅拷贝被克隆复用）
njs_flathsh_t            atom_hash;          // 私有表（每克隆独立）
njs_flathsh_t            *atom_hash_current; // 指向当前写入/查找目标
uint32_t                 shared_atom_count;  // 共享/私有的分界线
uint32_t                 atom_id_generator;  // 下一个新 atom 的 id
```

共享表的装填逻辑在 `njs_atom_hash_init`。[njs_atom.c:180-220](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_atom.c#L180-L220)：

```c
njs_flathsh_init(&vm->atom_hash_shared);
...
for (n = 0; n < NJS_ATOM_SIZE; n++) {
    value = &values[n];                      // values = &njs_atom[0]
    if (value->type == NJS_SYMBOL) {
        fhq.key_hash = njs_atom_symbol_key(value->string.atom_id);
        ret = njs_flathsh_unique_insert(&vm->atom_hash_shared, &fhq);
        ...
    }
    if (value->type == NJS_STRING) {
        start = value->string.data->start;
        len = value->string.data->length;
        fhq.key_hash = njs_atom_string_key(njs_djb_hash(start, len));
        ...
        ret = njs_flathsh_insert(&vm->atom_hash_shared, &fhq);
        ...
    }
    *njs_prop_value(fhq.value) = *value;
}
vm->atom_hash_current = &vm->atom_hash_shared;   // ← 模板期指向共享表
return NJS_ATOM_SIZE;
```

这里能看到 u2-l3 讲的 flathsh 的两种插入方式：符号用 `njs_flathsh_unique_insert`（只比 `key_hash`，因为每个符号 id 本就唯一），字符串用 `njs_flathsh_insert`（用 `njs_lexer_hash_test` 做逐字节比较，处理哈希冲突）。注意此刻的 `key_hash` 用了 `njs_atom_string_key` / `njs_atom_symbol_key`（[njs_atom.c:10-11](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_atom.c#L10-L11)）给哈希值打上「字符串/符号」标记——这是 flathsh 内部的区分手段，与 `atom_id` 的 number 标志位无关。

克隆时，分界线和私有表就这样建立。[njs_vm.c:421-424](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L421-L424)：

```c
nvm->shared_atom_count = vm->atom_id_generator;   // 分界线 = 模板当前的 id 总数
njs_flathsh_init(&nvm->atom_hash);                // 私有表清空
nvm->atom_hash_current = &nvm->atom_hash;         // 此后写入私有表
```

由于上一行 `*nvm = *vm`（[njs_vm.c:415](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L415)）是浅拷贝，`nvm->atom_hash_shared` 与模板共享同一份底层存储——这正是「只读资源复用」的实现。

查找时先私有后共享。[njs_atom.c:82-91](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_atom.c#L82-L91)：

```c
ret = njs_flathsh_find(vm->atom_hash_current, &fhq);   // 先查当前（私有）表
if (ret == NJS_OK) {
    return njs_prop_value(fhq.value);
}
ret = njs_flathsh_find(&vm->atom_hash_shared, &fhq);   // 再查共享表
if (ret == NJS_OK) {
    return njs_prop_value(fhq.value);
}
return NULL;
```

反向解码 `njs_atom_to_value` 则用 `shared_atom_count` 决定去哪张表取值。[njs_atom.h:52-66](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_atom.h#L52-L66)：

```c
if (atom_id < vm->shared_atom_count) {
    h = vm->atom_hash_shared.slot;          // 共享表
    ...
    *dst = *((njs_value_t *) njs_hash_elts(h)[atom_id].value);
} else {
    h = vm->atom_hash_current->slot;        // 私有表（current 已指向私有）
    atom_id -= vm->shared_atom_count;       // 扣掉分界线得到私有表内偏移
    ...
    *dst = *((njs_value_t *) njs_hash_elts(h)[atom_id].value);
}
```

#### 4.2.4 代码实践

**实践目标**：在源码层面追踪「同一个 `length` atom 在模板 VM 和克隆 VM 里指向同一份存储」。

**操作步骤**：

1. 读 `njs_vm_clone`（[njs_vm.c:392-434](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L392-L434)），确认 `*nvm = *vm` 之后没有对 `atom_hash_shared` 做任何重建。
2. 读 `njs_atom_hash_init` 末尾的 `vm->atom_hash_current = &vm->atom_hash_shared`，再读克隆里的 `nvm->atom_hash_current = &nvm->atom_hash`，对比两个指针的不同指向。
3. 假设 `length` 的 `atom_id` 是某个值 `L`（且 `L < shared_atom_count`），在两个 VM 里调用 `njs_atom_to_value` 都会走 `atom_hash_shared.slot` 的同一格。

**需要观察的现象**：

- 模板 VM 写新 atom 时，写进的是共享表（因为模板期 `current` 指向共享表）——这正是 `njs_builtin_objects_create` 在模板期注册内建对象属性、其键都进共享表的原因。
- 克隆 VM 写新 atom 时，写进的是私有表，绝不会动到共享表。

**预期结果**：能画出「模板 VM：共享表（写） → 克隆 VM：共享表（只读）+ 私有表（写）」的对照图。

**待本地验证**：若想直接观察，可构建 CLI 后用 `-d` 反汇编一段引用了 `length` 的代码，确认字节码里出现的是 `length` 的 atom_id（一个小整数），而不是字符串。

### 4.3 预定义原子常量

#### 4.3.1 概念说明

前面的 named atom 都是在运行期「按需新建」的，但有一大批原子是引擎一启动就固定存在的：所有关键字（`function`、`return`、…）、所有内建属性名（`length`、`prototype`、`constructor`、`name`、…）、所有已知符号（`Symbol.iterator`、`Symbol.asyncIterator`、…）。这些**预定义原子**满足三个特点：

1. 数量固定、内容固定，可以在编译期就列成一张清单。
2. 每个都有一个**有名字的常量**（`NJS_ATOM_STRING_length`、`NJS_ATOM_SYMBOL_iterator`），方便 C 代码直接引用，避免到处写字符串字面量。
3. 它们的 `atom_id` 就是清单里的序号（0, 1, 2, …），因此在模板 VM 装填后，它们的 id 都 `< NJS_ATOM_SIZE`，全部落在共享表里。

这张清单就是 `src/njs_atom_defs.h`。它的精妙之处在于：它本身**只是一堆宏调用**，不含任何真正的 C 定义。不同的地方 `#include` 它并预先 `#define` 不同的宏，就能把同一份清单展开成不同形态——枚举、静态值数组、初始化循环，全靠这一份清单驱动。这是 C 里常见的「X-Macro」技巧。

#### 4.3.2 核心流程

```text
njs_atom_defs.h（唯一数据源，~485 条 NJS_DEF_STRING/SYMBOL）
  │
  ├─ 在 njs_atom.h 里 #include + #define NJS_DEF_STRING(name,...) NJS_ATOM_STRING_##name,
  │     ⇒ 展开成枚举：NJS_ATOM_STRING_unknown=0, NJS_ATOM_STRING_length, …, NJS_ATOM_SIZE
  │
  └─ 在 njs_atom.c 里 #include + #define NJS_DEF_STRING(name,s,typ,tok) (njs_value_t){…预填好的字符串值…},
        ⇒ 展开成静态数组 njs_atom[]，每条都已填好 atom_id/token_type/字符串数据
```

`NJS_ATOM_SIZE` 既充当枚举的「哨兵」（其值 = 预定义原子总数），又是 `njs_atom_hash_init` 的返回值和 `atom_id_generator` 的起始值——一处定义，处处复用。

#### 4.3.3 源码精读

清单的「第 0 号」是一个特殊原子 `unknown`，它充当「尚未原子化」的哨兵。[njs_atom_defs.h:8](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_atom_defs.h#L8)：

```c
NJS_DEF_STRING(unknown, "\xFF\xFF", 0, NJS_TOKEN_ILLEGAL)
```

它对应枚举 `NJS_ATOM_STRING_unknown = 0`。注意它的字符串内容是两个 `0xFF` 字节——一个不可能出现在合法 JS 里的占位串。4.1 里看到的 `njs_value_atom(key) == 0` 判断（[njs_value.h:1059](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L1059)）正是用 `atom_id == 0 == NJS_ATOM_STRING_unknown` 来表示「这个键还没被原子化」。

枚举由 `njs_atom.h` 用 X-Macro 生成。[njs_atom.h:12-19](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_atom.h#L12-L19)：

```c
enum {
#define NJS_DEF_STRING(name, _1, _2, _3) NJS_ATOM_STRING_ ## name,
#define NJS_DEF_SYMBOL(name, str) NJS_ATOM_SYMBOL_ ## name,
#include <njs_atom_defs.h>
    NJS_ATOM_SIZE,
#undef NJS_DEF_SYMBOL
#undef NJS_DEF_STRING
};
```

于是 `length` → `NJS_ATOM_STRING_length`、`iterator` → `NJS_ATOM_SYMBOL_iterator`，C 代码里就能直接用这些有意义的常量名。

本讲实践任务要找的几个常用原子，都在清单里：

| 常量 | 清单位置 | 字符串内容 |
|---|---|---|
| `NJS_ATOM_STRING_length` | [njs_atom_defs.h:321](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_atom_defs.h#L321) | `"length"` |
| `NJS_ATOM_STRING_prototype` | [njs_atom_defs.h:356](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_atom_defs.h#L356) | `"prototype"` |
| `NJS_ATOM_STRING_constructor` | [njs_atom_defs.h:208](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_atom_defs.h#L208) | `"constructor"` |
| `NJS_ATOM_STRING_name` | [njs_atom_defs.h:335](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_atom_defs.h#L335) | `"name"` |
| `NJS_ATOM_SYMBOL_iterator` | [njs_atom_defs.h:13](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_atom_defs.h#L13) | `"Symbol.iterator"` |

注意 `NJS_DEF_SYMBOL` 只有 2 个参数（`name, str`），而 `NJS_DEF_STRING` 有 4 个（`name, str, 关键字类型, token id`）。像 `length`、`prototype` 这类不是关键字的字符串，后两个参数填 `0, 0`；而 `function`、`return` 这类关键字，后两个参数填 `NJS_KWD_RESERVED` 和对应的 `NJS_TOKEN_*`（见清单第 24–60 行的关键字段）。词法器（u3-l1）正是靠 `njs_atom[]` 里预填的 `token_type` / `token_id` 字段，把一个 atom 直接识别成关键字，省去单独的关键字查找表。

静态值数组 `njs_atom[]` 由 `njs_atom.c` 用同样的 X-Macro 展开，每条都已把 `type`、`atom_id`、`token_type`、字符串数据预填好。[njs_atom.c:16-34](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_atom.c#L16-L34)：

```c
const njs_value_t njs_atom[] = {
#define NJS_DEF_SYMBOL(_id, _s) njs_symval(_id, _s),
#define NJS_DEF_STRING(_id, _s, _typ, _tok) (njs_value_t) {
    .string = {
        .type = NJS_STRING,
        .truth = njs_length(_s) ? 1 : 0,
        .atom_id = NJS_ATOM_STRING_ ## _id,   // ← id 即枚举序号
        .token_type = _typ,
        .token_id = _tok,
        .data = & (njs_string_t) { .start = (u_char *) _s, ... },
    }
},
    #include <njs_atom_defs.h>
};
```

这张静态数组就是 `njs_atom_hash_init` 装填共享表时的数据来源（[njs_atom.c:178](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_atom.c#L178) 的 `values = &njs_atom[0]`）。整张清单合计 472 条字符串 + 13 条符号（共 485 条，加上哨兵 `NJS_ATOM_SIZE`），全部在编译期固化、模板期一次性装填进共享表。

#### 4.3.4 代码实践

**实践目标**：在清单文件里定位常用原子，理解 `NJS_DEF_STRING` 与 `NJS_DEF_SYMBOL` 两种声明的差异。

**操作步骤**：

1. 打开 `src/njs_atom_defs.h`，用搜索定位 `length`（第 321 行）、`prototype`（第 356 行）、`Symbol.iterator`（第 13 行）。
2. 对比这三行的宏形态：
   - `NJS_DEF_STRING(length, "length", 0, 0)` —— 4 参数，后两个 `0,0` 表示「不是关键字」。
   - `NJS_DEF_STRING(prototype, "prototype", 0, 0)` —— 同上。
   - `NJS_DEF_SYMBOL(iterator, "Symbol.iterator")` —— 2 参数，符号没有 token 类型。
3. 再找一行关键字，例如 `NJS_DEF_STRING(function, "function", NJS_KWD_RESERVED, NJS_TOKEN_FUNCTION)`（[njs_atom_defs.h:51](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_atom_defs.h#L51)），观察它后两个参数非零——这正是词法器能把它认成关键字的依据。
4. 打开 `src/njs_atom.h` 第 12–19 行，确认 `length` 展开后是 `NJS_ATOM_STRING_length`，`iterator` 展开后是 `NJS_ATOM_SYMBOL_iterator`。

**需要观察的现象**：

- 同一份 `njs_atom_defs.h`，在 `.h` 里展开成枚举、在 `.c` 里展开成值数组——改清单只需改一处，所有展开自动同步。
- `unknown` 永远是第 0 号，`atom_id == 0` 因此天然适合当「未原子化」哨兵。

**预期结果**：能回答「为什么在 C 代码里看到 `NJS_ATOM_STRING_length` 时，不需要去哈希表里查它——它就是一个编译期常量，值就是它在清单里的序号」。

#### 4.3.5 小练习与答案

**练习 1**：`njs_atom_is_number(0x80000000)` 的返回值是多少？它代表哪个整数的 number atom？

**参考答案**：返回 `0x80000000`（非零，判定为 number atom）。它代表整数 `0`：`njs_number_atom(0) = 0 | 0x80000000 = 0x80000000`。也就是说 `arr[0]` 的键 atom_id 是 `0x80000000`。

**练习 2**：为什么 `njs_atom_add` 在分配新 id 后要检查 `njs_atom_is_number(...)`？

**参考答案**：因为 named atom 的 id 由 `atom_id_generator++` 单调递增，且必须始终 `< 0x80000000`（否则会和 number atom 的位编码冲突）。一旦计数器自增到 `0x80000000`，最高位被置位，`njs_atom_is_number` 返回非零，说明 named atom 的 id 空间用尽，无法再安全分配——此时报「too many atoms」并失败。

**练习 3**：克隆 VM 里产生了一个新字符串 atom，它的 `atom_id` 会落在哪个范围？`njs_atom_to_value` 靠什么判断该去哪张表取它的值？

**参考答案**：会落在 `>= shared_atom_count` 的范围（私有表区）。`njs_atom_to_value` 用 `atom_id < vm->shared_atom_count` 这一条比较来分流：成立则去 `atom_hash_shared`，不成立则去 `atom_hash_current`（即私有表）并扣减 `shared_atom_count` 得到表内偏移。

## 5. 综合实践

把本讲三个最小模块串起来，完成一次「从 JS 源码到 atom_id」的完整追踪。

**任务**：解释下面这行 JS 在 njs 引擎里访问属性 `length` 时，键是如何以 atom_id 形式流动的。

```js
var n = "hello".length;
```

**追踪步骤**：

1. **编译期**：解析器看到 `.length`，词法器把 `length` 识别成一个 atom。因为 `length` 是预定义原子，编译器直接使用常量 `NJS_ATOM_STRING_length`（本讲 4.3 已确认它在 [njs_atom_defs.h:321](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_atom_defs.h#L321)）。字节码 `PROPERTY_GET` 指令的操作数里携带的就是这个 id。
2. **共享表装填**：模板 VM 启动时，`njs_atom_hash_init`（[njs_atom.c:168](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_atom.c#L168)）已把 `length` 装进 `atom_hash_shared`，其 id `< NJS_ATOM_SIZE`。
3. **执行期**：克隆 VM 执行这条 `PROPERTY_GET`，内层 `njs_property_query` 拿到的就是 `NJS_ATOM_STRING_length` 这个整数 id（4.1.3 已说明属性查找只传 atom_id）。由于该 id 不是 number atom（最高位为 0），也不是动态新建的私有 atom，它命中共享表，无需任何字符串比较。
4. **对比数字键**：如果把代码换成 `arr[3]`，4.1 讲过它走的是 number atom 快路径——id 直接是 `njs_number_atom(3) = 0x80000003`，连共享表都不查。

**交付物**：画出这条属性访问的「id 流动图」，标出 `length` 走 named atom（共享表命中）与 `3` 走 number atom（纯算术）两条路径的分叉点。

## 6. 本讲小结

- **atom = 驻留后的 32 位身份证**。njs 把所有属性名/标识符/符号驻留成 `atom_id`，使属性查找退化为整数比较，字节码里也只存 32 位 id 而非字符串。
- **number atom 用最高位编码**：`0x80000000` 置位表示这是个数字键，低 31 位就是整数本身（`njs_number_atom`/`njs_atom_number`/`njs_atom_is_number`，[njs.h:68-70](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs.h#L68-L70)）。小整数下标因此走纯算术快路径。
- **两张表 + 一个指针实现共享/私有分离**：模板 VM 用 `atom_hash_shared` 装预定义原子（`atom_hash_current` 指向它），克隆 VM 浅拷贝复用共享表、另起私有 `atom_hash`，并用 `shared_atom_count` 当分界线。
- **查找先私有后共享，解码按分界线分流**：`njs_atom_find` 先查 current 再查 shared；`njs_atom_to_value` 用 `atom_id < shared_atom_count` 决定取值位置。
- **预定义原子由一份 X-Macro 清单驱动**：`njs_atom_defs.h` 一份声明，在 `.h` 展开成枚举、在 `.c` 展开成静态值数组；`length`/`prototype`/`Symbol.iterator` 等都有 `NJS_ATOM_*` 常量名，C 代码可直接引用。
- **`unknown`（id=0）是「未原子化」哨兵**：`atom_id == 0` 表示键尚未原子化，触发 `njs_atom_atomize_key` 按需驻留。

## 7. 下一步学习建议

本讲把「值的键」层面讲透了，接下来可以沿两条线深入：

- **进入编译前端（u3 单元）**：u3-l1（词法器）会展示词法器如何直接复用 `njs_atom[]` 里预填的 `token_type`/`token_id` 把 atom 识别成关键字；u3-l3（变量与作用域）会展示 `njs_variable_t` 如何用 `atom_id` 标识一个变量名。这是 atom 在编译期的消费方。
- **进入对象模型（u5 单元）**：u5-l1（对象模型与属性）会展示 `njs_object_prop_t` 如何以 `atom_id` 为键存放在对象的属性哈希表里，以及 `njs_property_query` 沿原型链查找的具体过程。这是 atom 在运行期的消费方。

建议先读 `src/njs_atom_defs.h` 通览一遍清单，感受一下「整个引擎认识的全部名字」都在这一份文件里；再回到 `njs_atom.c` 对照本讲梳理的装填/查找/原子化三条路径，巩固理解。
