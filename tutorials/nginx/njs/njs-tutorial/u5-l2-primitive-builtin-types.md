# 原始内建类型：string / array / number

## 1. 本讲目标

本讲聚焦 njs 内置引擎里三个最常用、也最「贴地气」的内建类型：**字符串**、**数组**、**数字**。学完本讲你应该能够：

- 说清一个 njs 字符串值在内存里到底长什么样，为什么 `size` 和 `length` 是两个不同的字段，以及非 ASCII 字符串的「字符索引 → 字节偏移」是怎么快速完成的。
- 看懂 njs 数组的「快数组 / 慢数组」两条实现路径：什么时候元素住在一块连续数组里、什么时候退化成普通对象的哈希表，以及这条切换边界正是近期一个真实 bug 的根源。
- 理解一个 `double` 是如何被格式化成 JS 里的十进制字符串的，`njs_dtoa` 这个函数从哪来、做了什么，以及进制转换（`(255).toString(16)`）是怎么实现的。

这三个类型是后续所有内建对象（`RegExp`、`Date`、`JSON`、`TypedArray`……）的砖块，掌握它们的内部表示后，再读其它内建实现会轻松很多。

## 2. 前置知识

本讲默认你已经学过以下内容（这些是前序讲义建立的认知，此处不重复）：

- **`njs_value_t` 的 16 字节标签联合体**（见 u2-l2）。回忆一下：前 8 字节是头部（`atom_id`/`magic32`、`type`、`truth`、`magic16`），后 8 字节是 `payload` 联合，按类型复用——`number` 直接存 `double`，`string` 存一个指针，`array`/`object` 存对象指针。
- **对象模型与 `njs_object_t`**（见 u5-l1）。数组本质上「是一个对象」，它把 `njs_object_t` 作为第一个字段嵌进来，再额外加几个字段。`object.hash` 是 flathsh 私有哈希表，`fast_array` 是 `njs_object_t` 里的一个 1 bit 标志。
- **Atom 表**（见 u2-l4）。属性名/标识符被驻留成 32 位 `atom_id`，整数下标会被编码成 number atom（最高位 `0x80000000`）。

几个最小术语澄清：

- **UTF-8 字节数 vs 字符数**：UTF-8 是变长编码，ASCII 字符占 1 字节，中文等字符占 3 字节。`"hello"` 的字节数和字符数都是 5；`"你好"` 的字符数是 2、字节数是 6。JS 语义里 `String.prototype.length` 指的是「字符数」（更准确地说是 UTF-16 码元数，但 njs 在多数路径上按字符数处理）。
- **快路径 / 慢路径（fast/slow path）**：这是高性能 VM 的常见设计——为最常见的情形写一条「直连」的优化代码（快路径），把罕见情形留给通用但较慢的代码（慢路径）。njs 用 `njs_fast_path(...)` / `njs_slow_path(...)` 两个宏显式标注分支预测。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到什么 |
|---|---|---|
| [src/njs_string.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_string.h) | 字符串类型声明与辅助宏 | `njs_string_t` 结构、offset map 的设计注释、`NJS_STRING_MAP_STRIDE` |
| [src/njs_string.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_string.c) | 字符串实现（创建、属性、编码、索引） | `njs_string_alloc`/`njs_string_new`/`njs_string_create`、UTF-8 offset map 初始化与查找 |
| [src/njs_array.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_array.h) | 数组阈值常量与声明 | `NJS_ARRAY_FAST/LARGE` 阈值、`njs_array_push` |
| [src/njs_array.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_array.c) | 数组实现（分配、快慢转换、各原型方法） | `njs_array_alloc`、`njs_array_convert_to_slow_array`、`Array.prototype.slice` 的快/慢两条路径 |
| [src/njs_number.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_number.c) | 数字类型实现与 `Number` 原型方法 | `njs_number_to_string`、`njs_number_to_string_radix` |
| [src/njs_dtoa.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_dtoa.c) / [src/njs_dtoa.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_dtoa.h) | 从 QuickJS 移植的浮点打印/解析库 | `njs_dtoa` 包装、`JS_DTOA_FORMAT_*` 格式标志 |
| [src/njs_value.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h) | 值表示（回顾） | `string` 视图、`njs_is_fast_array` 宏 |
| [src/test/njs_unit_test.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/test/njs_unit_test.c) | C 单元测试 | 大数组 slice 的回归测试用例 |

---

## 4. 核心概念与源码讲解

### 4.1 字符串表示

#### 4.1.1 概念说明

字符串是 JS 里出现频率最高的值之一（属性名、日志、模板拼接都是它），所以它的内部表示必须既省内存又便于操作。njs 的字符串设计围绕两个核心矛盾展开：

1. **字节数 vs 字符数**：UTF-8 是变长编码，同一个字符串既要记「占多少字节」（决定分配多大、做字节级拷贝/拼接），又要记「有多少字符」（决定 `length`、`charAt(i)` 等 JS 语义）。
2. **「按字符下标取第 i 个字符」要快**：UTF-8 变长意味着「第 i 个字符」不能像 ASCII 那样直接 `start + i` 偏移取，理论上要从头扫描。njs 用一张可选的「偏移映射表（offset map）」把这件事摊还到接近 O(1)。

需要先澄清一个容易踩坑的地方：[src/njs_string.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_string.h) 文件顶部的注释还残留着「短字符串（≤14 字节）内联进 `njs_value_t`」的描述（见第 11–24 行）。但这是**历史遗留描述**——经过重构后，当前代码里 `NJS_STRING_SHORT` 这个宏并未定义，**所有字符串都统一走堆分配的 `njs_string_t`**。读源码时以实际代码为准，不要被旧注释误导。

#### 4.1.2 核心流程

一个 njs 字符串值的内存布局可以画成这样（数据都紧跟在 `njs_string_t` 头部之后）：

```
njs_value_t (16B)
└─ string.data ──► ┌──────────────────────── njs_string_t 头部 ────────────────────────┐
                   │ u_char   *start   ──┐                                      │
                   │ uint32_t length;   │  UTF-8 字符数                          │
                   │ uint32_t size;     │  字节数                                │
                   └────────────────────┘
                     │
                     ▼
                   ┌──────────────────────────────────────────────────────────┐
                   │  实际字节内容（size 字节）+ 末尾 '\0'                     │  ← start 指向这里
                   ├──────────────────────────────────────────────────────────┤
                   │  可选：UTF-8 offset map（uint32_t 数组）                  │
                   └──────────────────────────────────────────────────────────┘
```

几个要点：

- **`start` 指向头部之后的那块字节缓冲**，所以「头部 + 内容 + 可选 map」是一次性 `njs_mp_alloc` 连续分配出来的（少一次内存分配、对缓存友好）。
- **末尾永远补一个 `'\0'`**，让字符串可以直接喂给需要 C 字符串的 C 接口（见近期 commit `Ensuring string values are zero-terminated.`）。
- **offset map 只在必要时分配**：仅当「含多字节字符」且「字符数 > `NJS_STRING_MAP_STRIDE`（32）」时才有。map 的第 0 个元素为 `0` 时表示「尚未初始化」，第一次用到时才惰性构建。

offset map 的核心思想用一句话说：**每跨越 32 个字符，记一次当时的字节偏移**。这样要找「第 `i` 个字符」时：

\[
\text{字节偏移} = \text{map}\!\left[\left\lfloor i / 32 \right\rfloor - 1\right] + \text{在区间内继续走 } (i \bmod 32) \text{ 个字符}
\]

也就是先靠 map「大跳」到 `i` 之前最近的一个 32 倍数锚点，再小步走余数个字符。这样最坏也只扫 31 个字符，而不是从头扫 `i` 个。

#### 4.1.3 源码精读

先看类型本身。`njs_string_t` 非常精简，三个字段：

```c
struct njs_string_s {
    u_char    *start;
    uint32_t  length;   /* Length in UTF-8 characters. */
    uint32_t  size;
};
```
> 头部：字节起始指针、字符数、字节数。注意 `length`（字符）与 `size`（字节）的语义区分——这是整条字符串实现的主轴。见 [src/njs_string.h:73-77](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_string.h#L73-L77)。

`NJS_STRING_MAP_STRIDE = 32`，必须是 2 的幂，目的是把除法/取余换成移位与按位与：

```c
#define NJS_STRING_MAP_STRIDE  32
```
> offset map 的步长。见 [src/njs_string.h:35](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_string.h#L35)。关于「何时分配 map」的完整规则在第 60–66 行有注释说明（纯 ASCII 或字符数 ≤32 都不需要）。

值的层面，回顾 `njs_value_t` 的 `string` 视图——它用一个 `njs_string_t *data` 指针指向上述头部：

```c
struct {
    uint32_t          atom_id;
    njs_value_type_t  type:8;
    uint8_t           truth;
    uint8_t           token_type;
    uint8_t           token_id;
    njs_string_t      *data;
} string;
```
> 见 [src/njs_value.h:126-133](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L126-L133)。这里还顺手存了 `atom_id`（u2-l4 讲过：若该字符串已被驻留成 atom，这里记其 id；`NJS_ATOM_STRING_unknown` 表示「未原子化」）。

**分配**的核心是 `njs_string_alloc`，它一次性把「头部 + 内容 + 末尾 '\0' + 可选 map」全部算好并连续分配：

```c
u_char *
njs_string_alloc(njs_vm_t *vm, njs_value_t *value, uint64_t size,
    uint64_t length)
{
    uint32_t      total;
    njs_string_t  *string;

    if (njs_slow_path(size > NJS_STRING_MAX_LENGTH)) {
        njs_range_error(vm, "invalid string length");
        return NULL;
    }

    value->type = NJS_STRING;
    value->truth = size != 0;              // 空串的布尔真值预算为 false（与 "" == false 一致）
    value->atom_id = NJS_ATOM_STRING_unknown;

    total = njs_string_data_size(size, length);
    string = njs_mp_alloc(vm->mem_pool, sizeof(njs_string_t) + total);
    ...
    value->string.data = string;
    njs_string_data_init(string, size, length);
    return string->start;
}
```
> 见 [src/njs_string.c:188-219](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_string.c#L188-L219)。`njs_string_data_size` 算总字节数（含 map），`njs_string_data_init` 设置头部并在末尾写 `'\0'`、必要时把 map[0] 置 0 标记「未初始化」。

「要不要 map」的判定就在 `njs_string_data_size` / `njs_string_data_init` 里：

```c
uint32_t
njs_string_data_size(uint32_t size, uint32_t length)
{
    if (size != length && length > NJS_STRING_MAP_STRIDE) {     // 非纯 ASCII 且字符数 > 32
        return njs_string_map_offset(size + njs_length("\0"))
               + njs_string_map_size(length);
    }
    return size + njs_length("\0");
}
```
> 见 [src/njs_string.c:156-165](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_string.c#L156-L165)。`size != length` 就是「含多字节字符」的判定；`njs_string_map_size(length) = ((length-1)/32) * 4` 是 map 占的字节数。

**创建**时通常用更高层的 `njs_string_create`——它接受「原始字节」，自己判断是否纯 ASCII：

```c
njs_int_t
njs_string_create(njs_vm_t *vm, njs_value_t *value, const u_char *src, size_t size)
{
    u_char *p, *p_end;
    njs_str_t str;

    p = (u_char *) src;
    p_end = p + size;

    while (p < p_end && *p < 0x80) {   // 快速扫一遍：只要还有 ASCII 字节就继续
        p++;
    }

    if (p == p_end) {                  // 全程 ASCII → size == length，走最快路径
        return njs_string_new(vm, value, (u_char *) src, size, size);
    }

    str.start = (u_char *) src;        // 否则要用 UTF-8 解码算出真实字符数 length
    str.length = size;
    return njs_string_decode_utf8(vm, value, &str);
}
```
> 见 [src/njs_string.c:77-99](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_string.c#L77-L99)。这是一个典型的「先试快路径（纯 ASCII），不行再走慢路径」的模式。

**offset map 的惰性初始化**。当某个字符串第一次被按下标访问、且确实有 map 区域时，`map[0] == 0` 触发构建：

```c
void
njs_string_utf8_offset_map_init(const u_char *start, size_t size)
{
    ...
    offset = NJS_STRING_MAP_STRIDE;        // 从第 32 个字符开始记
    do {
        if (offset == 0) {
            map[n++] = p - start;          // 每满 32 个字符，记下当前字节偏移
            offset = NJS_STRING_MAP_STRIDE;
        }
        p = njs_utf8_next(p, end);         // 步进一个 UTF-8 字符（变长）
        offset--;
    } while (p < end);
}
```
> 见 [src/njs_string.c:2030-2056](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_string.c#L2030-L2056)。map[n] 存的是「第 (n+1)*32 个字符」对应的字节偏移。

**用 map 反查字节偏移**：

```c
const u_char *
njs_string_utf8_offset(const u_char *start, const u_char *end, size_t index)
{
    uint32_t *map;
    njs_uint_t skip;

    if (index >= NJS_STRING_MAP_STRIDE) {
        map = njs_string_map_start(end);
        if (map[0] == 0) {                                   // 惰性初始化
            njs_string_utf8_offset_map_init(start, end - start);
        }
        start += map[index / NJS_STRING_MAP_STRIDE - 1];     // 大跳到锚点
    }

    for (skip = index % NJS_STRING_MAP_STRIDE; skip != 0; skip--) {   // 小步走余数
        start = njs_utf8_next(start, end);
    }
    return start;
}
```
> 见 [src/njs_string.c:1959-1980](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_string.c#L1959-L1980)。这正是上一节那个公式的实现。

读字符串时，常用 `njs_string_prop` 把 `start/size/length` 抽到栈上一个轻量结构 `njs_string_prop_t`，避免反复解引用 `value->string.data`：

```c
size_t
njs_string_prop(njs_vm_t *vm, njs_string_prop_t *string, const njs_value_t *value)
{
    ...
    string->start = (u_char *) value->string.data->start;
    size = value->string.data->size;
    length = value->string.data->length;
    string->size = size;
    string->length = length;
    return (length == 0) ? size : length;   // 返回「有效长度」：字节串返回字节数，否则字符数
}
```
> 见 [src/njs_string.c:222-243](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_string.c#L222-L243)。注意开头那个 `value->string.data == NULL` 的分支：当字符串只存了 atom_id 而没物化 data 时，先用 `njs_atom_to_value` 把它物化（与 u2-l4 的 atom 机制衔接）。

#### 4.1.4 代码实践

**实践目标**：亲手观察「字节数 vs 字符数」在源码里是如何被区分对待的，并验证 offset map 的存在。

**操作步骤**：

1. 构建 CLI（若尚未构建，见 u1-l3）：
   ```bash
   ./configure && make njs
   ```
2. 用一段含中文的字符串跑 JS，从 `length` 与编码后字节数的差异体会上面的 `size`/`length`：
   ```bash
   ./build/njs -c 'var s="你好njs"; console.log(s.length); console.log(unescape(encodeURIComponent(s)).length)'
   ```
3. 阅读型跟踪：在 [src/njs_string.c:77](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_string.c#L77) 的 `njs_string_create` 上，跟着 `'你好'` 这个输入走：`*p < 0x80` 在第一个字节 `0xe4`（「你」的 UTF-8 首字节）处为假，于是跳出 ASCII 循环，转入 `njs_string_decode_utf8` 计算字符数。

**需要观察的现象**：
- `s.length` 输出 `5`（「你」「好」「n」「j」「s」共 5 个字符）。
- 第二个输出是 8（「你」「好」各 3 字节 + 3 个 ASCII 字节 = 9……实际取决于实现，请以本地输出为准）。

**预期结果**：字符数 < 字节数，说明这是一条「非纯 ASCII」字符串，在 `njs_string_data_size` 里会触发 `size != length` 分支；但因为它字符数 ≤ 32，**不会**分配 offset map。

> 待本地验证：上面第 2 步的精确字节数请以你的终端输出为准（`encodeURIComponent` 会把每个中文变成 `%xx%xx%xx`，长度可据此推算）。

#### 4.1.5 小练习与答案

**练习 1**：一个字符数为 100、全是 ASCII 的字符串，会分配 offset map 吗？为什么？

> **答案**：不会。判定条件是 `size != length && length > NJS_STRING_MAP_STRIDE`。纯 ASCII 时 `size == length`，第一个条件就为假，无论多长都不分配 map——因为 ASCII 下「第 i 个字符」直接 `start + i` 就能取到，根本不需要 map。

**练习 2**：为什么 offset map 的第 0 个元素被用作「未初始化」哨兵，而不是单独用一个 bool 字段？

> **答案**：第 0 个 map 元素记录的是「第 32 个字符」的字节偏移，而字符串第 0 个字符的字节偏移恒为 `0`。所以一个「已初始化」的 map，其 `map[0]` 必然 `> 0`（至少是第一个多字节字符的偏移）——用 `0` 当「未初始化」哨兵既不冲突又零额外开销，是典型的「用不可能出现的合法值当哨兵」技巧。

---

### 4.2 数组的快路径与慢路径

#### 4.2.1 概念说明

JS 的数组其实「是对象」：`a[3]` 在语义上就是去取对象 `a` 上键为 `"3"` 的属性。但「每个元素都走一次哈希表查找」对数组这种高频结构太慢了。所以绝大多数引擎（njs、V8、QuickJS 都是）都采用同一思路：**数组有两副面孔**。

- **快数组（fast array）**：元素连续存放在一块 `njs_value_t` 数组里，下标直接当数组索引用，O(1) 访问、缓存友好。这是「正常」数组的默认形态。
- **慢数组（slow array）**：退化为普通对象，元素存进 `object.hash` 哈希表，键是字符串化的整数下标。适合稀疏数组或被加了属性描述符的「奇怪」数组。

njs 用 `njs_object_t` 里的一个 1 bit 标志 `fast_array` 区分这两者。`njs_is_fast_array(value)` 这个宏就是「是数组且 `fast_array` 位置位」：

```c
#define njs_is_fast_array(value)  (njs_is_array(value) && njs_array(value)->object.fast_array)
```
> 见 [src/njs_value.h:602-603](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L602-L603)。整段 njs_array.c 里到处可见 `if (njs_is_fast_array(this)) { ...快路径... } else { ...慢路径... }` 的分叉，就是这个 bit 在驱动。

#### 4.2.2 核心流程

数组结构 `njs_array_t`：

```c
struct njs_array_s {
    njs_object_t  object;     /* 继承自对象：含 hash、shared_hash、__proto__、fast_array 标志 */
    uint32_t      size;       /* 缓冲区容量（含预留 spare） */
    uint32_t      length;     /* 逻辑长度 */
    njs_value_t   *start;     /* 有效数据起始（>= data，支持头部预留） */
    njs_value_t   *data;      /* 底层缓冲区真正起始 */
};
```

几条阈值常量决定了切换边界：

| 常量 | 值 | 含义 |
|---|---|---|
| `NJS_ARRAY_SPARE` | 8 | 创建快数组时额外预留的槽位数，避免 push 时频繁扩容 |
| `NJS_ARRAY_FAST_OBJECT_LENGTH` | 1024 | 「常规」数组长度的上限，许多方法用它判断能否走快路径 |
| `NJS_ARRAY_LARGE_OBJECT_LENGTH` | 32768 | **超过它就不再分配连续缓冲，直接建成慢数组** |
| `NJS_ARRAY_FLAT_MAX_LENGTH` | 1048576 | 强制 flat 分配的长度上限 |

> 见 [src/njs_array.h:11-19](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_array.h#L11-L19)。

整体流程：

```
创建数组 ──► njs_array_alloc
              │
              ├── flat 或 size <= 32768 ?
              │     ├── 是：分配 data 缓冲，fast_array = 1   【快数组】
              │     └── 否：data = NULL，fast_array = 0       【慢数组】
              │
使用中 ──────► 某些操作会把快数组「降级」为慢数组：
              • 把 length 设到 > 32768
              • Object.freeze/seal 一个快数组
              • 给元素加属性描述符（getter/setter、改 writable...）
              降级 = njs_array_convert_to_slow_array：把 start[] 里每个元素
              以 number atom 下标搬进 object.hash，再释放 data 缓冲。
```

#### 4.2.3 源码精读

**分配** `njs_array_alloc`——注意它在「大数组」时直接放弃连续缓冲：

```c
njs_array_t *
njs_array_alloc(njs_vm_t *vm, njs_bool_t flat, uint64_t length, uint32_t spare)
{
    ...
    size = length + spare;

    if (flat || size <= NJS_ARRAY_LARGE_OBJECT_LENGTH) {
        array->data = njs_mp_align(vm->mem_pool, sizeof(njs_value_t),
                                   size * sizeof(njs_value_t));   // 连续缓冲
    } else {
        array->data = NULL;                                       // 太大，不分配
    }

    array->start = array->data;
    ...
    array->object.fast_array = (array->data != NULL);             // 关键标志

    if (njs_fast_path(array->object.fast_array)) {
        array->size = size;
        array->length = length;
    } else {
        array->size = 0;
        array->length = 0;
        njs_set_array(&value, array);
        njs_array_length_redefine(vm, &value, length, 1);         // 慢数组：length 写进 hash
    }
    return array;
}
```
> 见 [src/njs_array.c:54-122](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_array.c#L54-L122)。`fast_array` 完全由「有没有分配 data」决定。

**降级** `njs_array_convert_to_slow_array`——把连续数组搬进哈希表：

```c
njs_int_t
njs_array_convert_to_slow_array(njs_vm_t *vm, njs_array_t *array)
{
    ...
    array->object.fast_array = 0;                       // 关掉标志
    length = array->length;

    for (i = 0; i < length; i++) {
        if (njs_is_valid(&array->start[i])) {           // 跳过稀疏空洞（invalid 槽位）
            prop = njs_object_property_add(vm, &value, njs_number_atom(i), 0);
            njs_value_assign(njs_prop_value(prop), &array->start[i]);
        }
    }

    njs_mp_free(vm->mem_pool, array->data);             // 释放连续缓冲
    array->start = NULL;
    return NJS_OK;
}
```
> 见 [src/njs_array.c:138-169](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_array.c#L138-L169)。注意 `njs_number_atom(i)`——把整数下标编码成 number atom 当哈希键（与 u2-l4 衔接）。

**触发降级的典型场景一**：把 `length` 设得过大。下面这段在 `Array.prototype` 的 length setter 里，超过 32768 就降级：

```c
    if (njs_fast_path(array->object.fast_array)) {
        if (njs_fast_path(length <= NJS_ARRAY_LARGE_OBJECT_LENGTH)) {
            ... 直接扩容连续缓冲 ...
        }

        ret = njs_array_convert_to_slow_array(vm, array);   // 超长 → 降级
        ...
    }
```
> 见 [src/njs_array.c:674-712](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_array.c#L674-L712)。

**触发降级的典型场景二**：冻结。`Object.freeze(a)` 要求每个元素都变成不可写、不可配置的属性——快数组的连续缓冲没法表达「每个槽位单独的属性描述符」，所以必须先降级成慢数组，再逐个属性设置标志：

```c
    if (njs_is_fast_array(value)) {
        array = njs_array(value);
        length = array->length;
        ret = njs_array_convert_to_slow_array(vm, array);   // freeze 前先降级
        ...
    }
```
> 见 [src/njs_object.c:1895-1908](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_object.c#L1895-L1908)。

**最重要的实战案例：`Array.prototype.slice` 的快/慢双路径**。`slice` 创建一个新数组，把源数组 `[start, start+length)` 窗口内的元素拷过去。问题在于：当结果长度很大（≥ 32761，即超过 `LARGE_OBJECT_LENGTH`）时，**结果数组本身是慢数组**，于是拷贝要走「慢路径（keys path）」。看 [src/njs_array.c:787](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_array.c#L787) 的 `njs_array_prototype_slice_copy`：

```c
array = njs_array_alloc(vm, 0, length, NJS_ARRAY_SPARE);   // length 可能很大 → 慢数组
...
if (njs_fast_path(array->object.fast_array)) {
    /* 快路径：连续缓冲，按下标直拷 */
    last = &array->start[length];
    for (value = array->start; value < last; value++, start++) {
        ret = njs_value_property_i64(vm, this, start, value);   // 直接按下标读源、写下标写目的
        ...
    }
    ...
}

/* 慢路径（keys path）：结果是非 fast 数组，要枚举源的所有整数下标 */
njs_set_array(&self, array);
keys = njs_array_indices(vm, this);                            // 枚举源数组所有自有整数键
...
for (n = 0; n < keys->length; n++) {
    idx = njs_string_to_index(&keys->start[n]);
    if (idx < start || idx >= start + length) {                // 过滤到窗口内
        continue;
    }
    ret = njs_value_property(vm, this, keys->start[n].atom_id, &val);
    ret = njs_value_property_i64_set(vm, &self, idx - start, &val);  // 写到目的的相对位置
    ...
}
```
> 见快路径 [src/njs_array.c:842-856](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_array.c#L842-L856)，慢路径（keys path）[src/njs_array.c:873-896](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_array.c#L873-L896)。

**这段慢路径正是近期一个真实 bug 的现场**。commit `66c6cdf9`（`Fix Array.prototype.slice() of large arrays in the non-fast keys path`）修复了它：旧版慢路径没有 `if (idx < start || idx >= start + length) continue;` 这层窗口过滤，而是把源的第 `idx` 个元素**原封不动写到目的的第 `idx` 个位置**——既忽略了 `[start, start+length)` 窗口，又忽略了「目的下标应当是 `idx - start`」。结果就是：对一个稀疏大数组 `slice(5, 45000)`，返回的数组里元素全跑到错误的位置上。修复同时移除了一段「dead fast object path」的死代码。这个案例完美诠释了为什么「两条路径」是 bug 的高发地带——**改了快路径别忘了同步慢路径**。

#### 4.2.4 代码实践

**实践目标**：用 commit `66c6cdf9` 引入的回归测试用例，亲手验证「大稀疏数组 slice」走的是慢路径，并体会快/慢路径的正确行为。

**操作步骤**：

1. 构建 CLI：`./configure && make njs`
2. 跑下面的脚本（这就是单元测试里 [src/test/njs_unit_test.c:5318-5323](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_unit_test.c#L5318-L5323) 的那条用例）：
   ```bash
   ./build/njs -c 'var a = []; a[10] = "a"; a[40000] = "b"; a.length = 50000;
   var s = a.slice(5, 45000);
   console.log([s.length, s[5], s[39995], (10 in s), (40000 in s)].join(","))'
   ```
3. 对比一个**小**数组的 slice（走快路径）：
   ```bash
   ./build/njs -c 'var a=[0,1,2,3,4,5,6,7]; console.log(a.slice(2,5).join(","))'
   ```

**需要观察的现象**：
- 第 2 步：源数组 `a` 在下标 10 和 40000 有值，length 被设为 50000（一个稀疏大数组）。`slice(5, 45000)` 应得到长度 `44995` 的数组，其中源的下标 10 落到目的下标 `10-5=5`（`s[5]==="a"`），源的下标 40000 落到目的 `40000-5=39995`（`s[39995]==="b"`）。
- 关键：目的数组长度 44995 > 32768，所以**结果数组是慢数组**，slice 内部走的就是上面那段 keys path。

**预期结果**：第 2 步输出 `44995,a,b,false,false`；第 3 步输出 `2,3,4`。
- `(10 in s)` 是 `false` 因为 `s` 的下标是相对的，真正的 `a[10]` 现在在 `s[5]`。
- 如果你在**未修复**的旧版本上跑，第 2 步会输出错误结果（`s[5]` 不是 `"a"`），这正是该 commit 修的 bug。

> 待本地验证：如果你的 `build/njs` 是当前 HEAD（f078f143）构建的，输出应如上；若要观察 bug 现象，可 `git stash`/切到 `66c6cdf9` 的父提交构建对比。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `njs_array_alloc` 在 `length > 32768` 时选择不分配连续缓冲，而不是无脑分配一个超大数组？

> **答案**：两方面的权衡。一是**内存**：一个 `njs_value_t` 是 16 字节，32768 个就占 512KB，稀疏数组里大部分槽位是空洞，连续分配会浪费巨量内存。二是**语义**：JS 允许稀疏数组（`a[1000000]=1` 只占用一个元素），慢数组用哈希表正好能「只有存了的键才占内存」。代价是访问慢一点，但对稀疏场景反而更省。

**练习 2**：`Object.freeze(arr)` 之后，`arr` 还是快数组吗？

> **答案**：不是。freeze 要让每个元素都变成不可写、不可配置，而快数组的连续缓冲没法给每个槽位单独挂属性描述符。所以 freeze 内部会先调 `njs_array_convert_to_slow_array` 把它降级成慢数组（见 [src/njs_object.c:1895-1908](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_object.c#L1895-L1908)），再逐个属性置标志。这也是为什么「冻结一个大数组」在 njs 里是个比较重的操作。

---

### 4.3 数字格式化

#### 4.3.1 概念说明

JS 里所有数字（整数、小数）都是 IEEE-754 双精度浮点数（`double`）。在 `njs_value_t` 里它就是 payload 里的那个 `double`，没有什么额外结构。本模块关心的是**反向问题**：怎么把这个 `double` 变成人能读的十进制字符串？

这里有个看似简单实则微妙的难点：**`0.1 + 0.2` 在 `double` 里并不精确等于 `0.3`**。如果你直接打印 `double` 的二进制位，会得到一长串数字；但 JS 规范（`Number.prototype.toString`）要求打印出**最短的、能唯一往返（round-trip）的十进制表示**——即「`0.3`」而不是「`0.30000000000000004`」之类。这需要专门的「最短精确浮点打印」算法（如 Grisu/Ryu 系列）。

njs 自己不实现这套算法，而是**直接复用 QuickJS 的实现**——`src/njs_dtoa.c` 是从 QuickJS 移植过来的「Tiny float64 printing and parsing library」（作者 Fabrice Bellard），文件头部的版权与来源 commit 写得很清楚。

#### 4.3.2 核心流程

数字到字符串的调度逻辑（`njs_number_to_string`）：

```
double num
   │
   ├── isNaN ?  ─►  "NaN"
   ├── +Infinity ?  ─►  "Infinity"
   ├── -Infinity ?  ─►  "-Infinity"
   └── 普通数 ─►  njs_dtoa(num, buf)   生成最短可往返十进制
```

进制转换（`(255).toString(16)` → `"ff"`）走另一条路 `njs_number_to_string_radix`，它复用同一个底层函数 `njs_dtoa2`，但传入 `radix` 参数并禁用指数表示。

`njs_dtoa` 的核心是一组「格式标志」，决定输出形态：

| 标志 | 含义 | 典型用途 |
|---|---|---|
| `JS_DTOA_FORMAT_FREE` | 最短可往返表示（默认） | `Number.prototype.toString` |
| `JS_DTOA_FORMAT_FRAC` | 固定小数位 | `Number.prototype.toFixed(n)` |
| `JS_DTOA_FORMAT_FIXED` | 固定整数位 | `toPrecision` 内部 |
| `JS_DTOA_EXP_ENABLED/DISABLED/AUTO` | 是否允许科学计数法 | 控制是否出现 `e` |
| `JS_DTOA_MINUS_ZERO` | 给 `-0` 显示负号 | 区分 `-0` 与 `+0` |

同一个 `njs_dtoa2`，靠这些标志拼出 `toString` / `toFixed` / `toPrecision` / `toExponential` 四个方法的不同行为。

#### 4.3.3 源码精读

文件来源（移植自 QuickJS 的依据）：

```c
/*
 * Tiny float64 printing and parsing library
 * Copyright (c) 2024 Fabrice Bellard
 * ...
 * bellard/quickjs/commit/dbbca3dbf3856938120071225a5e4c906d3177e8
 */
```
> 见 [src/njs_dtoa.c:1-25](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_dtoa.c#L1-L25)。这正是 commit `c7bd5f4a`（`Using printing and parsing library from QuickJS.`）引入的——njs 把数字的打印与解析都换成了 QuickJS 的成熟实现。

精度边界的注释（解释 `Number.MAX_SAFE_INTEGER` 的由来）：

```c
/*
 * 2^53 - 1 is the largest integer n such that n and n + 1
 * as well as -n and -n - 1 are all exactly representable
 * in the IEEE-754 format.
 */
#define NJS_MAX_SAFE_INTEGER  ((1LL << 53) - 1)
```
> 见 [src/njs_number.c:11-16](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_number.c#L11-L16)。`double` 的尾数是 52 位，能精确表示的整数上限就是 \( 2^{53} - 1 \)。

**主调度** `njs_number_to_string`——NaN/Infinity 走 atom 快查，普通数走 dtoa：

```c
njs_int_t
njs_number_to_string(njs_vm_t *vm, njs_value_t *string, const njs_value_t *number)
{
    double  num;
    size_t  size;
    u_char  buf[128];

    num = njs_number(number);

    if (isnan(num)) {
        njs_atom_to_value(vm, string, NJS_ATOM_STRING_NaN);          // "NaN" 是预定义 atom
    } else if (isinf(num)) {
        if (num < 0) {
            njs_atom_to_value(vm, string, NJS_ATOM_STRING__Infinity);
        } else {
            njs_atom_to_value(vm, string, NJS_ATOM_STRING_Infinity);
        }
    } else {
        size = njs_dtoa(num, (char *) buf);                          // 最短可往返十进制
        return njs_string_new(vm, string, buf, size, size);          // 纯数字 → size == length
    }
    return NJS_OK;
}
```
> 见 [src/njs_number.c:90-119](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_number.c#L90-L119)。注意 `"NaN"`/`"Infinity"` 直接复用预定义 atom（u2-l4），连字符串都不用新建。

**`njs_dtoa` 包装**——一行委托给 `njs_dtoa2`，默认「自由格式 + 十进制」：

```c
njs_inline size_t
njs_dtoa(double value, char *start)
{
    JSDTOATempMem  tmp_mem;
    return njs_dtoa2(start, value, 10, 0, JS_DTOA_FORMAT_FREE, &tmp_mem);
}
```
> 见 [src/njs_dtoa.h:94-100](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_dtoa.h#L94-L100)。`tmp_mem` 是算法内部的栈上临时内存（`uint64_t mem[37]`），避免堆分配。格式标志定义在 [src/njs_dtoa.h:40-52](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_dtoa.h#L40-L52)。

**进制转换** `njs_number_to_string_radix`——同一个 `njs_dtoa2`，改参数：

```c
static njs_int_t
njs_number_to_string_radix(njs_vm_t *vm, njs_value_t *string,
                           double number, uint32_t radix)
{
    ...
    len = njs_dtoa_max_len(number, (int) radix, 0,
                           JS_DTOA_FORMAT_FREE | JS_DTOA_EXP_DISABLED);   // 先算最大长度防溢出
    if (njs_slow_path((size_t) len + 1 > NJS_NUMBER_RADIX_BUF_SIZE)) {
        njs_internal_error(vm, "radix buffer overflow");
        return NJS_ERROR;
    }

    size = njs_dtoa2((char *) buf, number, (int) radix, 0,
                     JS_DTOA_FORMAT_FREE | JS_DTOA_EXP_DISABLED, &tmp_mem); // 禁用指数表示
    return njs_string_new(vm, string, buf, (uint32_t) size, (uint32_t) size);
}
```
> 见 [src/njs_number.c:601-624](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_number.c#L601-L624)。`(255).toString(16)` 时 `radix=16`，输出 `"ff"`；`JS_DTOA_EXP_DISABLED` 保证进制转换里不会冒出 `e` 记法。

**整数专用快路径**。当已知是整数时，有更轻量的转换函数（不必走完整浮点算法）：

```c
njs_int_t
njs_int64_to_string(njs_vm_t *vm, njs_value_t *value, int64_t i64)
{
    size_t  size;
    u_char  buf[128];
    size = njs_dtoa(i64, (char *) buf);          // 整数走 dtoa 也是高效的
    return njs_string_new(vm, value, buf, size, size);
}
```
> 见 [src/njs_number.c:122-130](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_number.c#L122-L130)。此外 `njs_u32toa`/`njs_i32toa`/`njs_u64toa`/`njs_i64toa`（见 [src/njs_dtoa.c:582-683](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_dtoa.c#L582-L683)）是纯整数的极简转换，常被 atom 表、数组下标等高频路径使用。

#### 4.3.4 代码实践

**实践目标**：验证 njs 的数字打印符合「最短可往返」语义，并对比进制转换。

**操作步骤**：

1. 构建 CLI：`./configure && make njs`
2. 验证最短往返（经典的 `0.1+0.2`）：
   ```bash
   ./build/njs -c 'console.log(0.1 + 0.2)'
   ```
3. 验证大整数边界与负零：
   ```bash
   ./build/njs -c 'console.log(Number.MAX_SAFE_INTEGER); console.log((-0).toString()); console.log((1/0).toString())'
   ```
4. 进制转换：
   ```bash
   ./build/njs -c 'console.log((255).toString(16)); console.log((255).toString(2)); console.log((3.5).toString(2))'
   ```

**需要观察的现象**：
- 第 2 步应输出 `0.30000000000000004`——注意这不是「打印精度问题」，而是 `0.1+0.2` 在 `double` 里的真实值；打印算法已经给出了能唯一往返回这个 `double` 的**最短**十进制串。
- 第 3 步：`9007199254740991`（即 \( 2^{53}-1 \)）、`0`（`-0` 的 `toString` 按规范是 `"0"`）、`Infinity`。
- 第 4 步：`ff`、`11111111`、`11.1`。

**预期结果**：如上。如果你想验证「最短往返」的威力，可以对比某些会输出超长尾数的语言行为——njs/QuickJS 的 dtoa 实现保证输出的是满足「读回来等于原 double」的最短串。

> 待本地验证：第 4 步 `(3.5).toString(2)` 的输出（二进制下 `3.5 = 11.1₂`）请以本地为准。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `njs_number_to_string` 对 `NaN` 和 `Infinity` 不调用 `njs_dtoa`，而是用 atom？

> **答案**：`NaN`/`Infinity` 是固定的字面量字符串，在 atom 表里早已预定义（`NJS_ATOM_STRING_NaN` 等，见 u2-l4）。直接复用 atom 既快（无需构造字符串、无需运行 dtoa 算法），又省内存（所有出现 `"NaN"` 的地方共享同一份）。这是「用驻留常量代替重复构造」的典型优化。

**练习 2**：`njs_dtoa` 和 `njs_number_to_string_radix` 都调用 `njs_dtoa2`，它们传的参数有何关键区别？

> **答案**：`njs_dtoa` 传 `radix=10`、`flags=JS_DTOA_FORMAT_FREE`（自由格式、允许必要的指数表示，用于通用 `toString`）；`njs_number_to_string_radix` 传调用者指定的 `radix`（2/8/16 等），并把 flags 设为 `JS_DTOA_FORMAT_FREE | JS_DTOA_EXP_DISABLED`——**禁用指数表示**，因为 `(n).toString(16)` 这类进制转换按规范不会产生科学计数法。同一个底层算法，靠这两个参数复用到多种 API。

---

## 5. 综合实践

把三个类型的知识串起来，做一个「迷你序列化器」阅读 + 验证任务。

**任务**：理解 njs 是如何把一个**混合了字符串、数组、数字**的值转成文本的（`console.log` / `JSON.stringify` 内部都要做这件事），并验证其中每一步用的是本讲讲的哪条路径。

**步骤**：

1. 构建并运行：
   ```bash
   ./build/njs -c 'var a = ["你好", 3.14, 255]; a[1000] = "尾"; console.log(JSON.stringify(a)); console.log(a.length)'
   ```
2. 阅读型跟踪，逐元素解释输出背后走了本讲的哪些机制：
   - `"你好"`：字符串元素 → 走 4.1 的 `njs_string_t`，`size=6, length=2`。
   - `3.14`：数字 → 走 4.3 的 `njs_dtoa`（最短可往返）。
   - `255`：整数 → 可能走整数快路径。
   - `a` 本身：因为 `a[1000]=...` 且 `length` 会变 1001（< 32768），它仍是**快数组**，但中间有大量稀疏空洞（`invalid` 槽位）。
3. 把 `a.length` 显式设到 40000 触发降级，再观察：
   ```bash
   ./build/njs -c 'var a = [1,2,3]; a.length = 40000; a[39999]="x";
   console.log(a.length, typeof a, a[0])'
   ```
   并结合 [src/njs_array.c:674-712](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_array.c#L674-L712) 解释：`a.length = 40000` 超过 32768，数组被 `njs_array_convert_to_slow_array` 降级为慢数组，之后 `a[0]` 的访问要走哈希表而不是连续缓冲。
4. （进阶）阅读 [src/njs_array.c:787](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_array.c#L787) 的 `njs_array_prototype_slice_copy`，回答：如果对上面这个被降级的慢数组做 `a.slice(0, 10)`，结果数组是快还是慢？为什么？

**预期结果**：
- 第 1 步输出形如 `["你好",3.14,255,null,...很多null...,null,"尾"]`（`JSON.stringify` 对稀疏空洞输出 `null`），`a.length` 为 `1001`。
- 第 3 步：数组降级后仍能正常访问，`a[0]` 返回 `1`，只是内部已是哈希表。

> 待本地验证：`JSON.stringify` 对稀疏数组空洞的确切输出（`null` vs 跳过）请以本地 njs 版本输出为准。

---

## 6. 本讲小结

- **字符串**：每个 njs 字符串值都持有一个堆分配的 `njs_string_t` 头部（`start` + `length` 字符数 + `size` 字节数），字节内容与可选的 UTF-8 offset map 紧跟其后一次性分配；`size != length` 标识「含多字节字符」，offset map 仅在「非 ASCII 且字符数 > 32」时才惰性分配，用于把字符下标快速映射到字节偏移。
- **数组**：有「快数组（连续 `start[]` 缓冲）」与「慢数组（退化成 `object.hash` 哈希表）」两条路径，由 `object.fast_array` 一个 bit 区分；`length > 32768`、`Object.freeze`、属性描述符等场景会触发 `njs_array_convert_to_slow_array` 降级。
- **slice 双路径是 bug 高发区**：结果数组本身的快/慢决定拷贝走快路径还是 keys path；commit `66c6cdf9` 修的正是慢路径丢失「窗口过滤 + 相对下标」导致的稀疏大数组 slice 结果错乱。
- **数字**：`double` 在 payload 里直接存放；`NaN`/`Infinity` 复用预定义 atom，普通数走从 QuickJS 移植的 `njs_dtoa`（最短可往返十进制）；进制转换复用同一 `njs_dtoa2`，靠 `radix` 与 `JS_DTOA_EXP_DISABLED` 参数区分。
- **共性设计哲学**：三个类型都体现了「为常见情形写快路径、罕见情形走通用慢路径」的 VM 设计思路（字符串的 ASCII 快扫、数组的连续缓冲、数字的整数专用转换），以及「用 atom 驻留常量、用惰性初始化省内存」的复用策略。

## 7. 下一步学习建议

- 本讲只覆盖了 `string`/`array`/`number` 三个最基础的内建类型。建议接着读 **u5-l3（内建对象与构造器的注册）**，看 `njs_string_instance_init` / `njs_array_type_init` / `njs_number_prototype_init` 这些初始化表是如何在 VM 启动时把本讲的方法（`slice`、`toString`、`toFixed`……）挂到原型上的——本讲引用的 `njs_number_prototype_properties[]`（[src/njs_number.c:627-651](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_number.c#L627-L651)）就是注册表的一个入口。
- 对二进制数据感兴趣的话，继续读 **u6-l3（Buffer 与 TypedArray）**，看 `njs_array_buffer_t` / `njs_typed_array_t`（[src/njs_value.h:195](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L195)）是如何在「数组」概念上扩展出定长二进制视图的——它们也复用了 `njs_object_t` 这一基座。
- 想深入浮点打印算法本身，可对照 QuickJS 上游 `njs_dtoa.c` 来源 commit（`bellard/quickjs@dbbca3db`）阅读 [src/njs_dtoa.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_dtoa.c)，理解「最短可往返」是如何用大整数运算保证正确性的。
