# Atom 表：字符串与符号的驻留

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 njs 为什么要把「属性名 / 标识符 / 已知符号」驻留（intern）成一个 32 位的 `atom_id`，而不是在引擎里到处拷贝字符串。
- 解释「数字 atom」的位编码：为什么整数下标 `0..2^31-1` 可以不占任何字符串存储，直接编码进 atom_id 的高位。
- 区分每个 VM 私有的 `atom_hash` 与跨克隆共享的 `atom_hash_shared`，并理解 `shared_atom_count` 这个「分界线」的作用。
- 在 `src/njs_atom_defs.h` 中快速定位 `'length'`、`'prototype'`、`Symbol.iterator` 等预定义常量，并理解它们在属性查找与词法分析中的作用。

本讲是单元二的收尾，承接 u2-l2（16 字节值表示）与 u2-l3（内存池与 flathsh）。你会发现：u2-l2 讲到的 `atom_id` 字段，正是本讲的主角；而 u2-l3 讲到的 `njs_flathsh_t`，正是 atom 表的底层容器。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**直觉一：字符串比较很贵，整数比较很便宜。** 在 JavaScript 引擎里，对象属性的读写、变量的查找、`for...in` 遍历几乎每一步都要「按键名匹配」。如果每次都拿字符串做 `memcmp`，开销会非常大。一个经典优化是**字符串驻留（string interning）**：全局只保留每个不同字符串的一份拷贝，给每份分配一个整数编号；此后引擎内部一律用这个整数编号（这里叫 `atom_id`）来代表这个字符串，比较两个键是否相等只需比较两个 32 位整数。

**直觉二：整数下标根本不需要字符串。** 访问 `arr[0]`、`arr[1]` 时，键其实是整数。如果能把这些「很小的整数键」直接编码进 atom_id 本身，就既不用驻留字符串，也不用查哈希表——这正是 njs 的做法，用 atom_id 的最高位来区分「这是一个数字」还是「这是一个字符串编号」。

**直觉三：预定义的东西可以先编译进二进制。** 引擎启动时就需要 `'length'`、`'prototype'`、`Symbol.iterator` 这类常量。与其运行时逐个生成，不如把它们在编译期就列成一张表，VM 启动时一次性装填。这张表就是 `src/njs_atom_defs.h`。

> 术语约定：本讲里 **atom（原子）** 指「被驻留的字符串或符号」，**atom_id** 指它对应的 32 位整数编号，**atom 表** 指存放这些 atom 的哈希表（底层就是 u2-l3 讲的 `njs_flathsh_t`）。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `src/njs_atom_defs.h` | 预定义原子清单：用 `NJS_DEF_STRING` / `NJS_DEF_SYMBOL` 宏逐条声明所有编译期常量（关键字、内建属性名、已知符号）。 |
| `src/njs_atom.h` | 由清单生成的枚举 `NJS_ATOM_STRING_*` / `NJS_ATOM_SYMBOL_*`；以及把 atom_id 反查回值的内联函数 `njs_atom_to_val`。 |
| `src/njs_atom.c` | atom 表的实现：`njs_atom[]` 静态数组、共享表装填 `njs_atom_hash_init`、查找 `njs_atom_find`、新增 `njs_atom_add` / `njs_atom_symbol_add`、把任意值原子化的 `njs_atom_atomize_key`。 |
| `src/njs.h` | 三个核心位运算宏 `njs_atom_is_number` / `njs_atom_number` / `njs_number_atom`。 |
| `src/njs_vm.h` | VM 结构体里的 atom 字段（`atom_hash_shared`、`atom_hash`、`atom_hash_current`、`shared_atom_count`、`atom_id_generator`）。 |
| `src/njs_vm.c` | `njs_vm_clone` 中如何划分共享 atom 与私有 atom 的分界线。 |
| `src/njs_lexer.c` | 词法分析器用 atom 表识别关键字与标识符的入口。 |

## 4. 核心概念与源码讲解

### 4.1 atom 驻留与编码

#### 4.1.1 概念说明

「原子化（atomize）」是指：拿一个字符串（或符号、整数键）进来，要么在表里找到它已存在的编号，要么给它分配一个新编号，最终返回一个 32 位的 `atom_id`。此后这个值在整个 VM 内部都由这个 `atom_id` 代表。

为什么要这样做？三点收益：

1. **省内存**：同一个属性名（比如几万个对象都有的 `'length'`）在内存里只存一份字符串，对象上只记一个 32 位 id。
2. **省比较**：判断两个键是否相等变成一次整数比较，而不是 `memcmp`。
3. **省分配**：整数下标连字符串都不用存（见 4.1.3 的数字 atom）。

#### 4.1.2 核心流程

把一段源码字符串原子化的大致流程（以词法分析器处理一个标识符为例）：

```
源码文本 "length"
   │  (逐字符累加 djb 哈希)
   ▼
njs_atom_find(text, hash)   ──► 先查当前 VM 的私有表，再查共享表
   │
   ├── 命中 ──► 直接拿到已有的 atom_id（0x...，高位为 0）
   │            └─ 若该 atom 的 token_type 是关键字类型，识别为关键字
   │              否则是普通标识符
   │
   └── 未命中 ──► njs_atom_add(text, hash)
            └─ 分配新 atom_id = atom_id_generator++
              插入当前 VM 的私有表 atom_hash
```

数字下标 `arr[3]` 走的是另一条更短的路径，见 4.1.3。

#### 4.1.3 源码精读：数字 atom 的位编码

这是本讲最精妙的设计。先看三个宏，它们全部在 `src/njs.h`：

[njs.h:68-70](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs.h#L68-L70) —— atom_id 的位编码三件套。下面这段说明每个宏在做什么：

```c
#define njs_atom_is_number(atom_id) ((atom_id) & 0x80000000)   // 测最高位
#define njs_atom_number(atom_id)    ((atom_id) & 0x7FFFFFFF)   // 屏蔽最高位，还原整数
#define njs_number_atom(n)          ((n) | 0x80000000)         // 把整数打包成 atom_id
```

它们的含义是：atom_id 的**最高位（bit 31）当作类型标志位**。

- 若最高位为 1（`atom_id & 0x80000000` 非零），说明这个 atom_id 不是字符串编号，而是一个**直接编码的整数键**；真正的整数就是低 31 位（`atom_id & 0x7FFFFFFF`）。
- 若最高位为 0，说明它是一个字符串/符号 atom 的**表内编号**（一个顺序递增的下标）。

用公式写得更清楚（设整数键为 \(n\)）：

\[
\text{atom\_id} \;=\; n \;\big|\; \text{0x80000000}, \qquad 0 \le n < 2^{31}
\]

反过来还原：

\[
n \;=\; \text{atom\_id} \;\&\; \text{0x7FFFFFFF}
\]

**位运算含义小结**：

- `njs_atom_is_number`：与上 `0x80000000`，结果非零即「是数字 atom」。它只是一个**判别**，不改变值。
- `njs_number_atom`：或上 `0x80000000`，把一个普通整数「打包」成数字 atom_id，相当于打上「我是数字」的标签。
- `njs_atom_number`：与上 `0x7FFFFFFF`（即清除最高位），从一个数字 atom_id 「拆」出原始整数。

为什么上限是 \(2^{31}\)？因为低 31 位最多表示 \(0\) 到 \(2^{31}-1\)，正好覆盖了 JS 数组能用的非负整数下标范围。下标 \(\ge 2^{31}\) 的极端情况会回退成普通字符串 atom。

这套编码带来的直接好处：**访问 `arr[0]` 时根本不查 atom 表**。属性查找代码在拿到整数下标后，只要它小于 `0x80000000`，就直接用 `njs_number_atom` 打包，跳过驻留：

[njs_value.h:1026-1027](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L1026-L1027) —— 整数下标的属性读取，直接打包成数字 atom，绕过字符串驻留：

```c
if (index < 0x80000000) {
    return njs_value_property(vm, value, njs_number_atom(index), retval);
}
```

反方向——给定一个 atom_id 要还原成 JS 值——由 `njs_atom_to_val` 完成，它最先判断的就是「是不是数字 atom」：

[njs_atom.h:39-50](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_atom.h#L39-L50) —— 若是数字 atom，用 `njs_atom_number` 拆出整数，转成字符串值返回（因为 JS 里 `arr[3]` 的键 `'3'` 也是字符串）：

```c
if (njs_atom_is_number(atom_id)) {
    num = njs_atom_number(atom_id);
    size = njs_dtoa(num, (char *) buf);
    ...
    dst->atom_id = atom_id;   // 但 atom_id 仍保留高位标志
    return NJS_OK;
}
```

#### 4.1.4 源码精读：原子化入口

当一个值需要被原子化时，统一入口是 `njs_atom_atomize_key`。它分三种类型处理：

[njs_atom.c:235-261](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_atom.c#L235-L261) —— 字符串值原子化：先尝试用 `njs_key_to_index` 把它当整数键处理（命中则走数字 atom 快路径），否则算哈希、查表、必要时新增：

```c
case NJS_STRING:
    num = njs_key_to_index(value);
    u32 = (uint32_t) num;

    if (njs_fast_path(u32 == num && (u32 < 0x80000000)
                      && !(num == 0 && signbit(num)))) {
        value->atom_id = njs_number_atom(u32);     // 整数串 → 数字 atom
    } else {
        hash_id = njs_djb_hash(value->string.data->start,
                               value->string.data->size);
        entry = njs_atom_find(vm, ...);             // 先查
        if (entry == NULL) {
            entry = njs_atom_add(vm, value, hash_id); // 查不到再加
        }
        *value = *entry;
    }
```

注意那个 `!(num == 0 && signbit(num))` 条件：它排除 `-0`。因为 `-0` 的整数值是 0，但 `-0` 不应被编码成数字 atom `0`（它会丢失负号语义），所以 `-0` 走普通字符串路径。

新增一个字符串 atom 时，`njs_atom_add` 从 `atom_id_generator` 取下一个递增编号：

[njs_atom.c:117-129](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_atom.c#L117-L129) —— 给新字符串分配 atom_id，并刻意避开数字 atom 的取值域（若新 id 不慎落到 `0x80000000` 以上会报「too many atoms」）：

```c
prop->u.value.string.atom_id = vm->atom_id_generator++;
if (njs_atom_is_number(prop->u.value.string.atom_id)) {
    njs_internal_error(vm, "too many atoms");
    return NULL;
}
```

#### 4.1.5 代码实践：观察数字 atom 的边界

**实践目标**：亲手验证 `njs_number_atom` / `njs_atom_number` 的位运算含义。

**操作步骤**：

1. 打开 `src/njs.h` 第 68–70 行，对照本讲 4.1.3 的解释，确认三个宏的写法。
2. 用纸笔或计算器手工演算：
   - `njs_number_atom(3)` 的结果（应当是 `0x80000003`）。
   - `njs_atom_number(0x80000005)` 的结果（应当是 `5`）。
   - `njs_atom_is_number(0x00000007)` 的结果（应当是 `0`，即「不是数字 atom」）。
3. 阅读本讲引用的 `njs_atom.c:240-241` 那个条件，回答：为什么 `u32 < 0x80000000` 是必要条件？如果去掉它会发生什么？

**需要观察的现象**：你会确认最高位就是「数字 vs 字符串编号」的判别位。

**预期结果**：数字 atom 的取值域是 `[0x80000000, 0xFFFFFFFF]`，字符串/符号 atom 的编号取值域是 `[0, 0x7FFFFFFF)`。

> 说明：以上是源码阅读 + 手工演算型实践，不需要运行 njs。如果你想运行验证，可构建 CLI（`./configure && make njs`）后用 `./build/njs -d -c 'var a=[1,2,3]; a[1]'` 观察反汇编中出现的 `0x8000xxxx` 形式的操作数，但具体反汇编格式待本地验证。

#### 4.1.6 小练习与答案

**练习 1**：`njs_atom_is_number(0x80000000)` 的结果是什么？它对应哪个整数键？

> **答案**：结果非零（真），表示这是数字 atom。`njs_atom_number(0x80000000) = 0x80000000 & 0x7FFFFFFF = 0`，所以它对应整数键 `0`。

**练习 2**：为什么 `njs_atom_add` 在分配新 atom_id 后要检查 `njs_atom_is_number`？

> **答案**：字符串/符号 atom 的编号必须落在 `[0, 0x7FFFFFFF)` 区间，否则会和数字 atom 的编码域冲突。当 `atom_id_generator` 增长到 `0x80000000` 时，新生成的 id 会被误判为数字 atom，所以此时报「too many atoms」并拒绝继续分配。

---

### 4.2 共享 vs 私有 atom 哈希

#### 4.2.1 概念说明

njs 支持把一个「模板 VM」克隆成多个「请求 VM」（详见 u2-l1 的克隆与隔离）。如果每个克隆都各自装填一遍那张庞大的预定义 atom 表（关键字 + 内建属性名，共数百条），既浪费内存又拖慢启动。

njs 的解法是**两张表 + 一条分界线**：

- **`atom_hash_shared`（共享表）**：在模板 VM 创建时装填一次，存放所有预定义 atom。克隆时所有 VM 共享同一份（因为它们 `*nvm = *vm` 浅拷贝，指针指向同一块共享结构，详见 u2-l1、u2-l3）。
- **`atom_hash`（私有表）**：每个克隆自己独有，存放该 VM 在运行期新产生的 atom（比如用户代码里出现的新标识符、`Symbol()` 创建的符号）。
- **`shared_atom_count`（分界线）**：一个 atom_id 编号。编号 `< shared_atom_count` 的属于共享表，`>= shared_atom_count` 的属于当前 VM 的私有表。

这样一来，模板 VM 把所有预定义 atom 编号成 `0..N-1`，克隆时把 `shared_atom_count` 设为 `N`，此后克隆自己新增的 atom 从 `N` 开始往后编，互不干扰。

#### 4.2.2 核心流程

```
模板 VM 创建期                          克隆期（per-request）
─────────────────                      ──────────────────────
njs_atom_hash_init()                   *nvm = *vm  (浅拷贝共享 atom_hash_shared)
  装填 atom_hash_shared                 shared_atom_count = 模板的 atom_id_generator
  从 0 开始编号                         atom_hash_current = &nvm->atom_hash  (切到私有表)
  返回 NJS_ATOM_SIZE                    atom_hash 初始化为空
atom_id_generator = NJS_ATOM_SIZE      后续 njs_atom_add 插入私有表
atom_hash_current = &atom_hash_shared
```

查找一个 atom 时，先查私有表，再查共享表。

#### 4.2.3 源码精读：VM 里的 atom 字段

[njs_vm.h:131-135](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.h#L131-L135) —— VM 结构体持有两表、一指针、一分界线、一生成器：

```c
njs_flathsh_t            atom_hash_shared;   // 跨克隆共享的预定义表
njs_flathsh_t            atom_hash;          // 当前 VM 私有的运行期表
njs_flathsh_t            *atom_hash_current; // 指向「当前该往哪查/插」的那张
uint32_t                 shared_atom_count;  // 共享/私有的 id 分界线
uint32_t                 atom_id_generator;  // 下一个可用 id
```

模板 VM 启动时，由内建对象初始化代码调用 `njs_atom_hash_init` 把 `atom_hash_shared` 装满：

[njs_builtin.c:150-151](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_builtin.c#L150-L151) —— 装填共享表，返回值（共多少条预定义 atom）成为 `atom_id_generator` 的起点：

```c
vm->atom_id_generator = njs_atom_hash_init(vm);
```

`njs_atom_hash_init` 本体遍历静态数组 `njs_atom[]`（即由 `njs_atom_defs.h` 生成的那张表），把每条字符串/符号插入共享表，最后把 `atom_hash_current` 指向共享表：

[njs_atom.c:180-220](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_atom.c#L180-L220) —— 装填共享表的关键片段（符号走 `unique_insert`，字符串走普通 `insert`）：

```c
njs_flathsh_init(&vm->atom_hash_shared);
...
for (n = 0; n < NJS_ATOM_SIZE; n++) {
    value = &values[n];
    if (value->type == NJS_SYMBOL) {
        fhq.key_hash = njs_atom_symbol_key(value->string.atom_id);
        ret = njs_flathsh_unique_insert(&vm->atom_hash_shared, &fhq);
        ...
    }
    if (value->type == NJS_STRING) {
        ...
        fhq.key_hash = njs_atom_string_key(njs_djb_hash(start, len));
        ret = njs_flathsh_insert(&vm->atom_hash_shared, &fhq);
        ...
    }
    *njs_prop_value(fhq.value) = *value;
}
vm->atom_hash_current = &vm->atom_hash_shared;
return NJS_ATOM_SIZE;
```

> 旁注：`njs_atom.c:10-11` 定义了两个键哈希宏：`njs_atom_symbol_key(hash) = (hash) | 0x80000000` 与 `njs_atom_string_key(hash) = (hash) & 0x7FFFFFFF`。注意这是 **flathsh 内部用来区分键类型**的哈希位，和 4.1 讲的 atom_id 高位编码是**两个不同语境**下对同一个 `0x80000000` 位的复用——这里用它把符号键与字符串键在哈希桶里隔开。

克隆时的分界线设置在 `njs_vm_clone`：

[njs_vm.c:421-424](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L421-L424) —— 把分界线钉在模板当前的 generator 值上，并把活动表切到私有表：

```c
nvm->shared_atom_count = vm->atom_id_generator;   // 分界线 = 共享表大小
njs_flathsh_init(&nvm->atom_hash);                // 私有表清空
nvm->atom_hash_current = &nvm->atom_hash;         // 此后查找/插入走私有表
```

查找逻辑在 `njs_atom_find`：先私有、后共享：

[njs_atom.c:82-90](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_atom.c#L82-L90) —— 两级查找，保证私有表里新增的同名 atom 优先于共享表里的预定义 atom：

```c
ret = njs_flathsh_find(vm->atom_hash_current, &fhq);
if (ret == NJS_OK) {
    return njs_prop_value(fhq.value);
}
ret = njs_flathsh_find(&vm->atom_hash_shared, &fhq);
if (ret == NJS_OK) {
    return njs_prop_value(fhq.value);
}
```

而 `njs_atom_to_val` 在按 atom_id 反查值时，正是用 `shared_atom_count` 决定查哪张表：

[njs_atom.h:52-66](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_atom.h#L52-L66) —— 编号小于分界线查共享表的 slot，否则减去分界线后查当前（私有）表的 slot：

```c
if (atom_id < vm->shared_atom_count) {
    h = vm->atom_hash_shared.slot;
    *dst = *((njs_value_t *) njs_hash_elts(h)[atom_id].value);
} else {
    h = vm->atom_hash_current->slot;
    atom_id -= vm->shared_atom_count;
    *dst = *((njs_value_t *) njs_hash_elts(h)[atom_id].value);
}
```

> 与 u2-l3 的联系：这里频繁出现的 `slot`、`njs_hash_elts(h)`、`njs_flathsh_find/insert` 全部是 u2-l3 讲过的扁平哈希表接口。atom 表本质上就是「把 `njs_value_t` 当作元素、用 djb 哈希当键」的一组 `njs_flathsh_t`。

#### 4.2.4 代码实践：画出两级查找

**实践目标**：理解「先私有后共享」与「分界线编号」如何配合。

**操作步骤**：

1. 阅读本讲引用的 `njs_vm.c:421-424`，写下克隆后 `shared_atom_count`、`atom_hash_current` 各自的值。
2. 假设模板 VM 装填了 600 条预定义 atom（编号 `0..599`），克隆 A 在运行期新增了 2 个标识符（编号 `600`、`601`），克隆 B 也新增了 2 个（同样是 `600`、`601`）。回答：
   - 克隆 A 的 `600` 和克隆 B 的 `600` 指向同一个 atom 吗？为什么这不会造成冲突？
   - 查找编号 `5` 会查哪张表？查找编号 `600` 又会查哪张表？
3. 对照 `njs_atom.h:52-66`，验证你的答案与代码一致。

**需要观察的现象**：两个克隆共享同一份预定义 atom，但各自的运行期 atom 完全隔离。

**预期结果**：克隆 A、B 的编号 `600` 互不相干（它们在各自的 `atom_hash` 私有表里，元素数组下标都是 `600 - shared_atom_count = 0`）；编号 `5` 查共享表，编号 `600` 查私有表。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `atom_hash_current` 要用指针，而不是直接固定指向 `atom_hash`？

> **答案**：在模板 VM 阶段，`atom_hash_current` 指向 `atom_hash_shared`（此时新增的预定义 atom 要进共享表）；克隆后它才指向私有的 `atom_hash`。用指针可以让同一套查找/插入代码（`njs_atom_find`/`njs_atom_add` 都用 `vm->atom_hash_current`）在两种阶段下都正确工作，无需分支。

**练习 2**：如果删除 `njs_atom_find` 里「先查私有表」那一步，只查共享表，会出什么问题？

> **答案**：运行期新产生的 atom（用户代码里的新标识符、`Symbol()` 等）只存在于私有表里，只查共享表会全部漏掉，导致查不到值（返回 NULL），进而触发重复新增或错误。

---

### 4.3 预定义原子常量

#### 4.3.1 概念说明

前面两节讲的是「机制」——atom 怎么编码、存哪张表。这一节讲「数据」——具体有哪些 atom 是预定义好的。

预定义 atom 分两类：

- **预定义字符串**：关键字（`function`、`return`…）、内建属性名（`length`、`prototype`、`constructor`…）、内建对象名（`Array`、`Promise`…）、错误消息等。它们的编号对应枚举常量 `NJS_ATOM_STRING_<名字>`。
- **预定义符号**：JS 规定的已知符号（`Symbol.iterator`、`Symbol.toPrimitive`…）。它们的编号对应枚举常量 `NJS_ATOM_SYMBOL_<名字>`。

这些常量的作用：让 C 代码里可以直接用 `NJS_ATOM_STRING_length` 这样的名字引用某个 atom_id，而不必记数字。属性查找、原型链遍历、内建对象初始化都大量使用它们。

#### 4.3.2 核心流程：从宏清单到枚举

预定义 atom 的生成是一个巧妙的「X-Macro」技巧：

1. `src/njs_atom_defs.h` 是一张纯数据清单，每行是一个宏调用，如 `NJS_DEF_STRING(length, "length", 0, 0)`、`NJS_DEF_SYMBOL(iterator, "Symbol.iterator")`。它本身不定义 `NJS_DEF_*` 宏。
2. 别的文件 `#include` 这张清单时，**先**把 `NJS_DEF_*` 定义成自己想要的形状，**再** include，就能让同一份清单在不同语境下生成不同的东西。
3. `src/njs_atom.h` 把 `NJS_DEF_STRING(name,...)` 定义成 `NJS_ATOM_STRING_ ## name,`、把 `NJS_DEF_SYMBOL(name,...)` 定义成 `NJS_ATOM_SYMBOL_ ## name,`，于是 include 后得到一个枚举，自动生成所有 `NJS_ATOM_STRING_*` / `NJS_ATOM_SYMBOL_*` 编号。
4. `src/njs_atom.c` 把同样的宏定义成「初始化一个 `njs_value_t` 静态对象」，于是 include 后得到 `njs_atom[]` 数组——枚举里第 n 项的编号，正好对应数组里第 n 项的预定义值。

#### 4.3.3 源码精读：枚举生成

[njs_atom.h:12-19](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_atom.h#L12-L19) —— 用 X-Macro 技巧，从清单生成枚举；末尾的 `NJS_ATOM_SIZE` 是预定义 atom 的总数：

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

清单本身（节选）：

[njs_atom_defs.h:8](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_atom_defs.h#L8) —— 第 0 号 atom 是占位用的 `unknown`，表示「尚未原子化」：

```c
NJS_DEF_STRING(unknown, "\xFF\xFF", 0, NJS_TOKEN_ILLEGAL)
```

[njs_atom_defs.h:13](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_atom_defs.h#L13) —— `Symbol.iterator` 对应的预定义符号：

```c
NJS_DEF_SYMBOL(iterator, "Symbol.iterator")
```

[njs_atom_defs.h:321](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_atom_defs.h#L321) —— `'length'` 字符串，0 表示非关键字、token id 为 0：

```c
NJS_DEF_STRING(length, "length", 0, 0)
```

[njs_atom_defs.h:356](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_atom_defs.h#L356) —— `'prototype'` 字符串：

```c
NJS_DEF_STRING(prototype, "prototype", 0, 0)
```

于是：`'length'` → `NJS_ATOM_STRING_length`，`'prototype'` → `NJS_ATOM_STRING_prototype`，`Symbol.iterator` → `NJS_ATOM_SYMBOL_iterator`。这三个常量在源码各处被直接引用，例如内建对象初始化时给原型挂 `constructor`、`length` 属性。

对应的静态值数组由 `njs_atom.c` 顶部的宏生成：

[njs_atom.c:16-34](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_atom.c#L16-L34) —— 同一份清单，这里把每条 `NJS_DEF_STRING` 展开成一个预填好的 `njs_value_t`（含 type、atom_id、token_type、指向字符串字面量的 `njs_string_t`），组成 `njs_atom[]` 数组：

```c
const njs_value_t njs_atom[] = {
#define NJS_DEF_SYMBOL(_id, _s) njs_symval(_id, _s),
#define NJS_DEF_STRING(_id, _s, _typ, _tok) (njs_value_t) {
    .string = {
        .type = NJS_STRING,
        .truth = njs_length(_s) ? 1 : 0,
        .atom_id = NJS_ATOM_STRING_ ## _id,
        .token_type = _typ,
        .token_id = _tok,
        .data = & (njs_string_t) { .start = (u_char *) _s, ... },
    }
},
    #include <njs_atom_defs.h>
};
```

符号值的构造略不同，走 `njs_symval` 宏——它把符号的描述串单独放，主值里只记类型与符号编号：

[njs_value.h:376-383](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L376-L383) —— 预定义符号值的内存布局，符号 id 存在 `magic32`：

```c
#define njs_symval(_sym_id, _s) {
    .data = {
        .type = NJS_SYMBOL,
        .truth = 1,
        .magic32 = NJS_ATOM_SYMBOL_ ## _sym_id,
        .u = { .value = (njs_value_t *) &njs_ascii_strval(_s) }
    }
}
```

#### 4.3.4 预定义 atom 在哪里被用到

**用途一：属性查找。** 对象属性读写最终都落到 `njs_value_property`，它直接取键值的 `atom_id` 去查对象哈希（u2-l3 讲的 `njs_flathsh_t`）：

[njs_value.h:1016](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L1016) —— 属性查找把 `key->atom_id`（可能正是某个 `NJS_ATOM_STRING_*`）直接交给属性查询：

```c
return njs_property_query(vm, pq, value, key->atom_id);
```

所以读 `arr.length` 时，`'length'` 早就是 `NJS_ATOM_STRING_length` 这个整数，整条链路全程没有字符串比较。

**用途二：词法分析的关键字识别。** 词法器扫到一个标识符后，用 `njs_atom_find` 查 atom 表；若命中的 atom 的 `token_type` 不是 `NJS_KEYWORD_TYPE_UNDEF`，就说明它是关键字或预定义标识符：

[njs_lexer.c:754-769](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_lexer.c#L754-L769) —— 词法器把标识符文本原子化，再据 `token_type` 区分关键字与普通标识符：

```c
entry = njs_atom_find(lexer->vm, token->text.start, token->text.length, hash_id);
if (entry == NULL) {
    ...
    entry = njs_atom_add(lexer->vm, &value, hash_id);   // 新标识符 → 新 atom
}
if (entry->string.token_type == NJS_KEYWORD_TYPE_UNDEF) {
    /* 普通标识符 */
} else {
    /* 关键字 / 预定义标识符，沿用预定义的 token_type/token_id */
}
```

这就解释了 `njs_atom_defs.h` 里关键字那一段的第四个参数（如 `NJS_TOKEN_FUNCTION`）的用途：它被预填进 atom 的 `token_id`，词法器命中后直接拿来用，不必再查关键字表。

#### 4.3.5 代码实践：定位三个常量并解释

**实践目标**：熟练在 `njs_atom_defs.h` 里查找预定义 atom，并理解其展开结果。

**操作步骤**：

1. 打开 `src/njs_atom_defs.h`，分别定位：
   - `'length'`（约第 321 行）→ 对应枚举 `NJS_ATOM_STRING_length`。
   - `'prototype'`（约第 356 行）→ 对应枚举 `NJS_ATOM_STRING_prototype`。
   - `Symbol.iterator`（第 13 行）→ 对应枚举 `NJS_ATOM_SYMBOL_iterator`。
2. 打开 `src/njs_atom.h` 第 12–19 行，确认这些枚举常量是如何由 `NJS_DEF_STRING` / `NJS_DEF_SYMBOL` 宏经 `##` 拼接生成的。
3. 在 `src/njs_atom.c` 第 16–34 行，对照 `NJS_DEF_STRING` 的展开，确认 `njs_atom[NJS_ATOM_STRING_length]` 这一格里的 `atom_id`、`token_type`、`data->start` 分别是什么。

**需要观察的现象**：你会看到同一份 `njs_atom_defs.h` 清单，在 `.h` 里变成了枚举编号、在 `.c` 里变成了静态值数组，两者下标严格对齐。

**预期结果**：`NJS_ATOM_STRING_length` 是某个整数编号 n；`njs_atom[n].string.data->start` 指向字符串字面量 `"length"`；`token_type` 为 0（非关键字）。`NJS_ATOM_SYMBOL_iterator` 经 `njs_symval` 展开后，`magic32` 字段就是它自己的编号。

#### 4.3.6 小练习与答案

**练习 1**：为什么 `njs_atom_defs.h` 里的宏清单能同时生成「枚举」和「静态值数组」两样东西？

> **答案**：因为它用的是 X-Macro 模式——清单本身只写数据（`NJS_DEF_STRING(length, "length", 0, 0)`），不定义宏。包含它的文件先按自己的需要 `#define NJS_DEF_STRING(...)`，再 include 清单。`.h` 把宏定义成枚举项，`.c` 把宏定义成初始化器，于是同一份清单生成两种产物，且两者的第 n 项天然对应同一个 atom。

**练习 2**：`'length'` 在 `njs_atom_defs.h` 里的第四个参数是 `0`，而 `'function'` 是 `NJS_TOKEN_FUNCTION`。这个差异有什么后果？

> **答案**：第四个参数被填进 atom 的 `token_id`。词法器扫描到 `function` 时，`njs_atom_find` 命中预定义 atom，读到非 `NJS_KEYWORD_TYPE_UNDEF` 的 token_type/非零 token_id，直接识别为关键字；而扫描到 `length` 时 token_type 为 0（`NJS_KEYWORD_TYPE_UNDEF`），会被当作普通标识符处理。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个「跟踪一个属性读的全过程」任务。

**任务**：解释 JS 代码 `var a = {x: 1}; a.x` 在 njs 内部从源码到属性读取，atom 机制分别扮演了什么角色。

**建议步骤**：

1. **编译期（对应 4.3）**：词法器扫描到标识符 `a` 和属性名 `x`。
   - 假设 `x` 不是预定义 atom，追踪 `njs_lexer.c:754-767`：`njs_atom_find` 未命中 → `njs_atom_add` 给 `x` 分配一个新 atom_id（从 `atom_id_generator` 取）。
   - 追踪生成的 AST/字节码里，`a.x` 这条属性读指令的操作数存的是 `x` 的 atom_id（一个整数），而不是字符串 `"x"`。
2. **运行期属性查找（对应 4.1 + 4.2）**：执行到读 `a.x` 时，引擎拿到键的 atom_id。
   - 在 `njs_value.h:1016` 看到，属性查询直接用 `key->atom_id`。
   - 若键是整数下标（如 `a[0]`），则走 4.1.3 的数字 atom 快路径，连 atom 表都不查。
3. **多请求隔离（对应 4.2）**：若这段代码跑在 NGINX 里，每个请求是一个克隆 VM。
   - 指出 `shared_atom_count` 把 `x` 这种运行期 atom 隔离在每个请求自己的 `atom_hash` 里，而 `'length'`、`'prototype'` 等预定义 atom 则跨请求共享。

**产出**：画一张时序图，标出「源码文本 → djb 哈希 → njs_atom_find/njs_atom_add → atom_id → 字节码操作数 → njs_value_property → 对象哈希查表」这条链路上每一步涉及的源码行号（用本讲给出的永久链接）。

> 如果本地已构建 `build/njs`，可用 `./build/njs -d -c 'var a={x:1}; a.x'` 对照反汇编输出验证操作数，但具体反汇编格式与操作数显示待本地验证。

## 6. 本讲小结

- **atom = 被驻留的字符串/符号**，用一个 32 位 `atom_id` 代表；引擎内部比较键、存属性名都走整数，不再到处拷贝、比较字符串。
- **数字 atom 用最高位编码**：`njs_number_atom(n) = n | 0x80000000` 把整数下标直接打包进 atom_id，`njs_atom_is_number` 测最高位判别，`njs_atom_number` 屏蔽最高位还原——整数键 thus 完全不占字符串存储、不查表。
- **两张表 + 一条分界线**：`atom_hash_shared`（预定义，跨克隆共享）与 `atom_hash`（运行期，每 VM 私有），由 `shared_atom_count` 划分编号域，`atom_hash_current` 指针让同一套代码在模板期与克隆期都能工作。
- **预定义 atom 用 X-Macro 生成**：`njs_atom_defs.h` 是一份纯数据清单，在 `.h` 里展开成 `NJS_ATOM_STRING_*` / `NJS_ATOM_SYMBOL_*` 枚举，在 `.c` 里展开成 `njs_atom[]` 静态值数组，两者下标严格对齐。
- **预定义 atom 支撑两大热路径**：属性查找（`njs_value_property` 直接用 `key->atom_id`）与词法关键字识别（`njs_atom_find` 命中后读预填的 `token_type`）。
- **与 u2-l2/u2-l3 的衔接**：atom_id 正是 `njs_value_t` 头部那个字段；atom 表底层就是 `njs_flathsh_t`，其内存由 VM 的 `mem_pool` 统一持有。

## 7. 下一步学习建议

本讲把「值如何被命名/按键」讲透了。接下来两步：

- **进入编译前端（单元三）**：去看词法器 `src/njs_lexer.c` 如何系统性地调用 `njs_atom_find`/`njs_atom_add` 把整段源码切成 token 流并原子化（u3-l1）。本讲 4.3.4 的词法器片段就是那讲的入口。
- **进入执行引擎（单元四）**：去看 `NJS_VMCODE_PROPERTY_GET` 等指令在解释器主循环里如何用 atom_id 完成对象属性读写（u4-l1、u4-l2）。届时你会看到本讲的 `njs_value_property` 与 u2-l3 的 `njs_flathsh_t` 在字节码层的真正汇合点。

如果想再深入 atom 表本身，可以阅读 `src/njs_atom.c` 里尚未展开的 `njs_atom_symbol_add`（运行期 `Symbol()` 的驻留）与 `njs_atom_atomize_key` 的 `NJS_NUMBER` 分支（数字值转字符串 atom 的边界处理），它们是本讲机制的直接延伸。
