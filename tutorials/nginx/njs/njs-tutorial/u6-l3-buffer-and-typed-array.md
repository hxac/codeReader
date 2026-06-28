# Buffer 与 TypedArray 的双引擎实现

## 1. 本讲目标

本讲讲解 njs 中「二进制数据处理」的内部实现。学完之后，你应该能够：

- 说清楚在内置 njs 引擎里 `Buffer`、`TypedArray`、`ArrayBuffer` 三者的内存结构，以及为什么 **Buffer 本质上就是一个换了原型链的 `Uint8Array`**。
- 说清楚在 QuickJS 引擎里 `Buffer` 是如何被「挂在 `Uint8Array` 原型之上」包装出来的，以及它为什么和内置引擎的实现路径完全不同。
- 对照三个真实的近期修复（`concat()` 类型混淆、`readInt/writeInt` 越界、`fill` 零长度死循环，以及同类型拷贝忽略视图偏移），说出它们各自属于哪一类**边界条件**，并能从源码定位到对应的判断分支。

本讲只讨论「数据怎么存、怎么拷、怎么校验」，不展开编码（hex/base64）细节，也不涉及 NGINX 集成。

## 2. 前置知识

阅读本讲前，建议你已经掌握 u6-l1（QuickJS 包装层）和 u6-l2（双引擎模块模式）建立的认知：

- **双引擎 = 双份代码**：每个扩展功能在 `external/` 或 `src/` 下成对存在 `njs_*.c` 与 `qjs_*.c`，前者基于内置引擎的 `njs_value_t`，后者基于 QuickJS 的 `JSValue`。
- **值类型不同**：内置引擎用一个 16 字节的 `njs_value_t` 承载所有 JS 值（见 u2-l2），对象值在 payload 里存一个堆指针；QuickJS 用 `JSValue`（Tag + 值），TypedArray 由 QuickJS 自己的内建类管理。
- **对象类型枚举**：内置引擎为每个内建类型编号，见 `NJS_OBJ_TYPE_*`（u5-l3）。
- **内存池**：内置引擎所有运行时分配都挂在 `vm->mem_pool` 上（见 u2-l3）。

如果你已经读过 u5-l2（原始内建类型）会更好，因为 `Buffer` 在内置引擎里就复用了 `Uint8Array` 的快数组表示。下面用到的几个术语先约定：

| 术语 | 含义 |
|---|---|
| ArrayBuffer | 一段**原始字节**的容器，是所有 TypedArray/Buffer 的「后端存储」。 |
| TypedArray | 一个**视图（view）**：在某个 ArrayBuffer 上以某种元素类型（如 `Uint8`、`Int32`、`Float64`）读取字节。 |
| Buffer | Node.js 风格的字节容器。在 njs 里它**就是 `Uint8Array`**，只是换了一组「Buffer 专属」的方法（`readInt32LE`、`toString`、`concat` 等）。 |
| 视图偏移 (offset) | 一个视图不一定要从 ArrayBuffer 第 0 字节开始；`offset` 记录它从哪开始。 |
| detached buffer | 一个被「摘除」的 ArrayBuffer：其底层数据指针被置空，再访问就要报错。 |

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|---|---|
| [src/njs_value.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h) | 定义 `njs_array_buffer_t`、`njs_typed_array_t` 结构，以及 `njs_is_typed_array` / `njs_is_detached_buffer` 等判定宏。 |
| [src/njs_vm.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.h) | 定义 `NJS_OBJ_TYPE_*_ARRAY` 对象类型枚举。 |
| [src/njs_array_buffer.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_array_buffer.c) | 内置引擎：ArrayBuffer 的分配、`detach`、`slice`、写时复制。 |
| [src/njs_array_buffer.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_array_buffer.h) | 内联函数 `njs_array_buffer_slice`、`njs_array_buffer_size`。 |
| [src/njs_typed_array.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_typed_array.c) | 内置引擎：TypedArray 的构造、分配、`slice`/`sort`/`reverse` 等。 |
| [src/njs_typed_array.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_typed_array.h) | `njs_typed_array_element_size`、`length`、`offset`、`start`、`prop` 等内联工具。 |
| [src/njs_buffer.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_buffer.c) | 内置引擎：`Buffer` 模块的全部方法（`alloc`/`from`/`concat`/`readInt`/`writeInt`/`fill` 等）。 |
| [src/njs_buffer.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_buffer.h) | 编码描述符 `njs_buffer_encoding_t` 等。 |
| [src/qjs_buffer.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs_buffer.c) | QuickJS 侧：`Buffer` 模块的全部实现。 |
| [src/qjs.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs.c) | QuickJS 包装层；提供 `qjs_typed_array_data` 共享工具。 |
| [src/qjs.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs.h) | `QJS_CORE_CLASS_ID_BUFFER` 等类 id 枚举。 |
| [test/buffer.t.js](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/test/buffer.t.js) | Buffer 的 test262 风格回归测试（含三个修复的用例）。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **内置引擎的 Buffer/TypedArray/ArrayBuffer**：数据怎么存、怎么分配。
2. **QuickJS 的 Buffer**：另一套完全不同的实现路径。
3. **边界条件案例**：从真实 bug 看二进制操作里的四类典型陷阱。

### 4.1 内置引擎的 Buffer/TypedArray/ArrayBuffer

#### 4.1.1 概念说明

在内置 njs 引擎里，二进制数据由两层结构组成：

- **`njs_array_buffer_t`**：后端存储。它持有一个 `union` 指针 `u`（可以按 `u8`/`u16`/`u32`/`f64` 等不同宽度去解释同一段内存），以及这段内存的 `size`（字节数）。
- **`njs_typed_array_t`**：视图。它持有一个指向后端的 `buffer` 指针，以及三个关键字段：`offset`（视图起点，单位是「元素」）、`byte_length`（视图占多少字节）、`type`（元素类型枚举）。

最关键的认知是：**`Buffer` 不是一个独立结构**。在内置引擎里，一个 `Buffer` 就是一个 `njs_typed_array_t`，它的 `type` 被设成 `NJS_OBJ_TYPE_UINT8_ARRAY`，只是把原型链（`__proto__`）从「Uint8Array 原型」换成「Buffer 原型」，从而挂上一组 Buffer 专属方法。换句话说：

> **Buffer = Uint8Array 的实例 + Buffer 的原型链。**

这一点决定了：所有 TypedArray 的通用机制（快数组表示、`offset`/`byte_length`、detach）都自动适用于 Buffer。

#### 4.1.2 核心流程

分配一个 Buffer 的流程：

1. 调 `njs_buffer_alloc(size, zeroing)`。
2. 它内部调 `njs_typed_array_alloc(vm, &value, 1, zeroing, NJS_OBJ_TYPE_UINT8_ARRAY)`。
3. `njs_typed_array_alloc` 调 `njs_array_buffer_alloc(vm, size, zeroing)` 分配后端字节；后端用 `union u` 暴露多种宽度的视图。
4. 再 `njs_mp_zalloc` 出一个 `njs_typed_array_t`，填好 `buffer`、`offset=0`、`byte_length=size`、`type`。
5. 回到 `njs_buffer_alloc`，把 `array->object.__proto__` 改写成 Buffer 原型——这一步就是「从 Uint8Array 变身成 Buffer」的全部魔法。

视图与字节数的关系：一个长度为 \(L\)、元素宽度为 \(E\) 的视图，其

\[
\text{byte\_length} = L \times E
\]

而视图在 buffer 中的字节起点是 `offset × E`。元素宽度 \(E\) 由类型决定（`Uint8`/`Int8` 是 1，`Uint16` 是 2，`Float64` 是 8，见 `njs_typed_array_element_size`）。

#### 4.1.3 源码精读

先看两个核心结构。`njs_array_buffer_s` 持有那段字节，并用一个 `union` 提供多种解释方式；`njs_typed_array_s` 持有视图的三要素：

[src/njs_value.h:195-221](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L195-L221) — 后端 `njs_array_buffer_t` 的 `union u`（`u8`/`u16`/`u32`/`f64`… 都指向同一段字节）与视图 `njs_typed_array_t` 的 `buffer`/`offset`/`byte_length`/`type` 字段。

判定一个值是不是 TypedArray、是不是 Buffer、buffer 是不是被 detach，都靠这几个宏：

[src/njs_value.h:606-624](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L606-L624) — `njs_is_array_buffer`、`njs_is_typed_array`、`njs_is_detached_buffer`（判 `(buffer)->u.data == NULL`）以及 `njs_is_typed_array_uint8`（再校验 `type == NJS_OBJ_TYPE_UINT8_ARRAY`）。注意 detach 的判定非常朴素：底层数据指针被置空就算 detached。

每个内建数组类型都有一个 `NJS_OBJ_TYPE_*` 编号，TypedArray 家族是连续的一段：

[src/njs_vm.h:54-65](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.h#L54-L65) — `NJS_OBJ_TYPE_TYPED_ARRAY_MIN/MAX` 圈出 9 种 TypedArray（`UINT8_ARRAY` … `FLOAT64_ARRAY`），`NJS_OBJ_TYPE_ARRAY_BUFFER`、`NJS_OBJ_TYPE_DATA_VIEW`、`NJS_OBJ_TYPE_BUFFER` 则是独立的类型编号。

后端 ArrayBuffer 的分配——所有字节都从 `vm->mem_pool` 来，且支持可选清零：

[src/njs_array_buffer.c:10-62](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_array_buffer.c#L10-L62) — `njs_array_buffer_alloc` 先拒掉超过 `UINT32_MAX` 的大小，再用 `njs_mp_zalloc`/`njs_mp_alloc` 分配 `size` 字节，挂到 `array->u.data`。

「摘除」一个 buffer 就是把它的数据指针和大小清零——这是后面好几个边界条件的根源：

[src/njs_array_buffer.c:245-265](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_array_buffer.c#L245-L265) — `njs_array_buffer_detach` 把 `buffer->u.data = NULL; buffer->size = 0;`，于是 `njs_is_detached_buffer` 之后会返回真。

现在看本模块最核心的一段：**Buffer 其实就是换了原型的 Uint8Array**。

[src/njs_buffer.c:193-210](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_buffer.c#L193-L210) — `njs_buffer_alloc` 调 `njs_typed_array_alloc(..., NJS_OBJ_TYPE_UINT8_ARRAY)` 造一个 Uint8Array，然后把 `array->object.__proto__` 改写成 Buffer 原型。返回的 `njs_typed_array_t*` 与普通 Uint8Array 在内存布局上没有任何区别。

`njs_typed_array_alloc` 根据入参（数字长度 / 已有 ArrayBuffer / 已有 TypedArray / 类数组对象）算出 `size`，再分配后端和视图。注意第 158 行的「同类型快拷贝」——这是模块三的一个 bug 现场：

[src/njs_typed_array.c:139-159](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_typed_array.c#L139-L159) — 造好视图后，若来源是同类型 TypedArray，直接 `memcpy` 一段 `size` 字节。来源指针的取法见 4.3 节。

#### 4.1.4 代码实践

> **实践目标**：亲手验证「Buffer 就是 Uint8Array」这一论断，并理解结构字段。

1. 构建内置引擎 CLI（如 `./configure && make njs`，详见 u1-l3）。这一步在本环境未执行，**待本地验证**。
2. 运行（**待本地验证**，因为未实际构建）：
   ```bash
   ./build/njs -c 'var b = Buffer.from([1,2,3]); console.log(b instanceof Uint8Array, b.length, b.byteLength)'
   ```
3. **需要观察的现象**：应输出 `true 3 3`——说明 Buffer 确实是 Uint8Array 的实例，且 `length` 与 `byteLength` 相等（因为元素宽度是 1）。
4. **预期结果**：`true 3 3`。若你的 njs 默认走 QuickJS（看 `njs.engine`），结论同样成立，但内部实现路径不同（见 4.2）。

> 这是一个「待本地验证」的运行型实践：本讲写作时未在本环境实际构建运行。若无法构建，可改为**源码阅读型实践**：在 [src/njs_buffer.c:193-210](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_buffer.c#L193-L210) 中确认 `njs_buffer_alloc` 唯一与「普通 Uint8Array」不同的就是最后那行改 `__proto__`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `njs_is_detached_buffer` 只判 `u.data == NULL` 就够了，而不需要专门的标志位？

**参考答案**：因为 `njs_array_buffer_detach`（[src/njs_array_buffer.c:259](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_array_buffer.c#L259)）就是通过把 `u.data` 置空、`size` 清零来实现「摘除」的，所以「指针为空」与「已摘除」是等价条件，不必额外加标志位。

**练习 2**：一个 `Float64Array` 视图的 `offset` 字段存的是「字节偏移」还是「元素偏移」？

**参考答案**：存的是**元素偏移**。从 [src/njs_value.h:218](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L218) 的注释（`byte_offset / element_size`）和 [src/njs_typed_array.h:57-61](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_typed_array.h#L57-L61) 的 `njs_typed_array_offset`（返回 `offset * element_size`）可以看出，真正取字节地址时还要再乘以元素宽度。

### 4.2 QuickJS 的 Buffer

#### 4.2.1 概念说明

QuickJS 侧的实现思路完全不同。QuickJS 自己已经内建了 `ArrayBuffer` / `TypedArray`（那是引擎核心的一部分，不在 njs 源码里），所以 njs 在 QuickJS 上**不需要重新发明 TypedArray**，只需要做两件事：

1. 定义一个 **Buffer 类**（`QJS_CORE_CLASS_ID_BUFFER`），用来「打标签」标识某个对象是 Buffer。
2. 把 Buffer 的原型方法挂上去，并让 **Buffer 的原型链继承自 Uint8Array 的原型**。

这样，在 QuickJS 里 `Buffer` 同样是 `Uint8Array` 的「子类」（通过原型链），但它底层的数据结构和生命周期由 QuickJS 引擎自己管理，njs 只负责「贴方法、读字节、写字节」。

#### 4.2.2 核心流程

QuickJS 侧 Buffer 的初始化（在 `qjs_buffer_builtin_init` 里完成）：

1. `JS_NewClass(QJS_CORE_CLASS_ID_BUFFER, &qjs_buffer_class)`：注册一个空的 Buffer 类（无自定义 finalizer，因为 QuickJS 自己管内存）。
2. 建一个普通对象 `proto`，把 Buffer 原型方法（`readInt32LE`/`write`/`fill` 等）挂上去。
3. 取出全局的 `Uint8Array` 构造器，拿到它的原型 `ta_proto`，把 `proto` 的原型设成 `ta_proto`——这就建立了 `Buffer proto → Uint8Array proto → TypedArray proto → Object` 的原型链。
4. 把 `proto` 绑成 `QJS_CORE_CLASS_ID_BUFFER` 的类原型。
5. 造一个 `Buffer` 函数对象（实际上调它会被拒绝，提示用 `Buffer.alloc()`/`Buffer.from()`），挂到全局对象上。

读取一段 Buffer 的字节时，njs 不直接碰内部结构，而是调共享工具 `qjs_typed_array_data`——它通过 QuickJS 的 C API `JS_GetTypedArrayBuffer` 一次性拿到「底层 ArrayBuffer 指针 + 视图偏移 + 视图长度」，再算出真正的字节起点。这种「一次取全」的设计，是后面 4.3 节 concat bug 在 QuickJS 侧天然不存在的原因。

#### 4.2.3 源码精读

先看 Buffer 类与模块注册结构。注意类 id 从 64 起，刻意避开 QuickJS 内建的 1..63：

[src/qjs.h:26](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs.h#L26) — `QJS_CORE_CLASS_ID_BUFFER = 64`，njs 自定义类 id 的起点（与 u6-l1 讲的「从 64 起」一致）。

[src/qjs_buffer.c:242-260](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs_buffer.c#L242-L260) — `qjs_buffer_class` 是个「空壳」类（`finalizer = NULL`，因为底层字节的释放交给 QuickJS），而 `qjs_buffer_module` 用的是 QuickJS 模块注册结构 `qjs_module_t{name, init}`（与 u6-l2 讲的 `njs_module_t{preinit, init}` 不同）。

原型链的搭建——`proto` 继承自 Uint8Array 的原型：

[src/qjs_buffer.c:2661-2734](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs_buffer.c#L2661-L2734) — `qjs_buffer_builtin_init` 注册 Buffer 类、把 Buffer 原型方法挂到 `proto`、用 `JS_SetPrototype(proto, ta_proto)` 接上 Uint8Array 原型链，最后把 `Buffer` 函数挂到全局对象。第 2692-2693 行还通过「造一个 Uint8Array 实例再 `JS_GetClassID`」动态取得 QuickJS 内部 Uint8Array 的 class id。

读取字节的共享工具——一次调用同时取到指针、偏移、长度：

[src/qjs.c:1208-1243](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs.c#L1208-L1243) — `qjs_typed_array_data` 用 `JS_GetTypedArrayBuffer` 一次性拿到底层 ArrayBuffer 与视图的 `byte_offset`/`byte_length`，再 `data->start += byte_offset; data->length = byte_length;` 算出真正可读的字节区间。这一步「取值即校验」，是 4.3 节 TOCTOU 在 QuickJS 侧不存在的根本原因。

QuickJS 侧的 `concat` 就建立在这个工具上——每次取元素都现取现校验，没有「先统计长度、后拷贝」的两段式：

[src/qjs_buffer.c:432-529](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs_buffer.c#L432-L529) — `qjs_buffer_concat`：第一遍循环调 `qjs_typed_array_data` 累加长度；第二遍循环再次调 `qjs_typed_array_data` 取指针并 `njs_cpymem`。两遍都用同一个「取值即校验」的入口，天然避免了内置引擎那种「校验过、但拷贝时类型已变」的窗口。

#### 4.2.4 代码实践

> **实践目标**：验证 QuickJS 侧 Buffer 同样是 Uint8Array 的「子类」，并理解原型链。

1. 构建 QuickJS 版 CLI（`./configure --with-quickjs ... && make njs`，**待本地验证**）。
2. 运行（**待本地验证**）：
   ```bash
   ./build/njs -n QuickJS -c 'console.log(njs.engine, Buffer.from([1,2]) instanceof Uint8Array)'
   ```
3. **需要观察的现象**：应输出 `QuickJS true`。
4. **预期结果**：`QuickJS true`。即便实现路径不同，JS 层面的语义（Buffer 是 Uint8Array 子类）在两引擎下一致。

> 同样，本运行步骤在本环境未实际执行，属「待本地验证」。源码阅读型替代：在 [src/qjs_buffer.c:2697-2699](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs_buffer.c#L2697-L2699) 找到 `JS_SetPrototype(ctx, proto, ta_proto)`，确认 Buffer 原型确实挂在 Uint8Array 原型之下。

#### 4.2.5 小练习与答案

**练习 1**：为什么 QuickJS 侧的 `qjs_buffer_class.finalizer` 是 `NULL`？

**参考答案**：因为 Buffer 的底层字节由 QuickJS 自己的 ArrayBuffer 管理，QuickJS 会在 ArrayBuffer 被 GC 时负责释放。njs 的 Buffer 类只是「贴方法、打标签」，不持有额外需要释放的资源，所以不需要自定义 finalizer（见 [src/qjs_buffer.c:242-245](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs_buffer.c#L242-L245)）。

**练习 2**：QuickJS 侧取一个 Buffer 的可读字节区间，需要哪两个信息？

**参考答案**：底层 ArrayBuffer 的数据指针（`JS_GetArrayBuffer` 返回）和视图的偏移+长度（`JS_GetTypedArrayBuffer` 返回的 `byte_offset`/`byte_length`）。真正起点 = 数据指针 + `byte_offset`，可读长度 = `byte_length`（见 [src/qjs.c:1231-1240](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs.c#L1231-L1240)）。

### 4.3 边界条件案例：从四个真实修复看二进制陷阱

二进制操作是内存安全 bug 的高发区。下面用近期四个真实修复（三个在任务中点名，一个作为同类补充），说明它们各自属于哪一类边界条件。每个修复都**同时改了 `njs_buffer.c` 和 `qjs_buffer.c` 两份**（模块二讲过的「双改」铁律）。

四个案例的边界条件分类一览：

| 案例 | commit | 边界条件类别 | 后果 |
|---|---|---|---|
| ① `concat()` 类型混淆 | `943a9f35` | **TOCTOU**（校验与使用之间状态被改） | 野指针、越界读 |
| ② `readInt/writeInt` 越界 | `e0712408` | **边界值遗漏**（漏掉 0） | 越界读写 6 字节 |
| ③ `fill` 零长度死循环 | `8b0a1a87` | **零长度 / 无进度循环** | worker 挂死 |
| ④ 同类型拷贝忽略偏移 | `210bd6b1` | **视图偏移被忽略** | 拷错数据、越界读 |

#### 4.3.1 案例①：`concat()` 的 TOCTOU

**概念**：TOCTOU（Time-Of-Check-To-Time-Of-Use）指「检查时」与「使用时」之间存在时间窗口，期间状态被改变。内置引擎的 `njs_buffer_concat` 原本分两遍：第一遍（长度统计）校验每个元素都是合法的 TypedArray；第二遍（拷贝）重新读取元素并强转为 TypedArray 使用。问题在于：如果一个数组元素的取值会触发 getter（非快数组 + 访问器属性），getter 在两遍之间可以返回**不同的值**——第一遍返回合法的 TypedArray 骗过校验，第二遍返回一个普通对象或一个被 detach 的 buffer，强转后得到野指针，造成越界读。

**修复方式**：在第二遍拷贝时**重新校验**类型与 detach 状态，与第一遍一致。

[src/njs_buffer.c:914-941](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_buffer.c#L914-L941) — 拷贝循环里，重新 `njs_value_property_i64` 取值后，注释 `/* The getter above may have changed the element type. */` 紧跟 `njs_is_typed_array(&val)` 与 `njs_is_detached_buffer(arr->buffer)` 两道重新校验，校验不过即抛 `TypeError`。QuickJS 侧因为用 `qjs_typed_array_data`「取值即校验」，本来就不存在这个窗口（见 4.2）。

回归测试很好地展示了攻击手法——getter 第一次返回合法 TypedArray、第二次返回别的值：

[test/buffer.t.js:101-145](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/test/buffer.t.js#L101-L145) — `concatRevalidate_tsuite` 用 `Object.defineProperty(list, 0, { get() {...} })` 构造一个会在两次访问间「换值」的元素，期望 `Buffer.concat(list)` 抛 `TypeError`。

#### 4.3.2 案例②：`readInt/writeInt` 的越界（漏掉 `byteLength == 0`）

**概念**：变量长度的 `readIntLE`/`readUIntBE` 等方法接受一个 `byteLength` 参数（1~6）。原校验只拒 `byteLength > 6`，**漏掉了 `byteLength == 0`**。当 `byteLength == 0` 时：边界检查 `size + index > byte_length` 退化为 `index > byte_length`（因为 `size` 是 0），可以通过；而随后 `switch (size)` 落入默认的 6 字节分支，于是从攻击者可控的偏移处读/写 6 字节——越界。

**修复方式**：把 `size > 6` 改成 `size == 0 || size > 6`，即要求 `byteLength ∈ [1, 6]`。

[src/njs_buffer.c:1025-1036](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_buffer.c#L1025-L1036) — `njs_buffer_prototype_read_int`：`if (njs_slow_path(size == 0 || size > 6))` 抛 `"byteLength" must be >= 1 and <= 6`；随后 `size + index > array->byte_length` 才是有效的边界检查。

[src/njs_buffer.c:1311-1322](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_buffer.c#L1311-L1322) — `njs_buffer_prototype_write_int` 同样的 `size == 0 || size > 6` 修复。

QuickJS 侧同一处：

[src/qjs_buffer.c:1181-1190](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs_buffer.c#L1181-L1190) — `qjs_buffer_prototype_read_int`：`if (size == 0 || size > 6)` 抛 RangeError，随后 `size + index > self.length` 边界检查。

#### 4.3.3 案例③：`fill` 零长度源导致的死循环

**概念**：`Buffer.prototype.fill` 用一个 TypedArray 作为填充源时，循环每次推进 `n = min(byte_length, end - to)` 字节。当**源是空 TypedArray**（`byte_length == 0`）时，`n` 恒为 0，`to` 永远不前进，`while (to < end)` 永远成立——死循环，worker 卡死。

**修复方式**：源为空时直接把目标区间清零后返回，与 `fill_string`（空字符串清零）的行为对齐。

[src/njs_buffer.c:1919-1925](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_buffer.c#L1919-L1925) — `njs_buffer_fill_typed_array`：取到源 `byte_length` 后，`if (byte_length == 0) { memset(to, 0, end - to); return NJS_OK; }`，提前结束。对照 [src/njs_buffer.c:1887-1890](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_buffer.c#L1887-L1890) 的 `njs_buffer_fill_string`（空字符串也是 `memset` 清零），两者现在一致。

回归测试：

[test/buffer.t.js:343-344](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/test/buffer.t.js#L343-L344) — 新增 `{ value: Buffer.from(''), expected: '\0\0\0' }`（空 TypedArray 源）与 `value_from_buf: [1, 1]` 两个用例，验证空源会零填充而非死循环。

#### 4.3.4 案例④（同类补充）：同类型拷贝忽略视图偏移

**概念**：当一个 TypedArray 是某个更大 buffer 的**子视图**（`subarray`/带 `byteOffset` 构造）时，它的有效数据并不在底层 buffer 的第 0 字节，而在 `offset × element_size` 处。原本几处「同类型快拷贝」直接从底层 buffer 第 0 字节 `memcpy`，忽略了源视图的偏移——于是拷到了错误的数据，甚至读到视图之外的相邻数据。

**修复方式**：用 `njs_typed_array_start()` / `njs_typed_array_offset()` 取源视图的真实起点。

[src/njs_typed_array.c:150-159](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_typed_array.c#L150-L159) — `njs_typed_array_alloc` 的同类型快拷贝从 `&src_tarray->buffer->u.u8[0]` 改为 `njs_typed_array_start(src_tarray)`（见 `njs_typed_array_start` 的定义 [src/njs_typed_array.h:64-68](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_typed_array.h#L64-L68)，它返回 `&array->buffer->u.u8[offset × element_size]`）。

> 这个案例虽不在任务点名列表，但它与案例①②③同属「视图语义被忽略」一类边界条件，且同样在 `njs_typed_array.c` 中，有助于你建立「视图 = buffer + 偏移 + 长度」的牢固心智。

#### 4.3.5 代码实践

> **实践目标**：把四个修复与源码位置一一对应，并说清每个属于哪类边界条件。这是本讲的主实践（源码阅读型）。

1. 用 `git show <commit>` 查看四个修复的完整 diff：
   ```bash
   git show e0712408     # readInt/writeInt 越界
   git show 943a9f35     # concat 类型混淆
   git show 8b0a1a87     # fill 零长度死循环
   git show 210bd6b1     # 同类型拷贝忽略偏移
   ```
2. 对每个修复，定位到本讲列出的源码行，确认修复前后只改了一两个条件分支。
3. 对照下面的「判定标准」填写每个案例的边界条件类别：
   - 出现「校验过、使用时已变」→ TOCTOU（案例①）。
   - 出现「取值范围边界值（如 0）漏判」→ 边界值遗漏（案例②）。
   - 出现「循环步长可能为 0」→ 零长度/无进度循环（案例③）。
   - 出现「视图有 offset 却从第 0 字节算」→ 视图偏移被忽略（案例④）。
4. **需要观察的现象**：四个 commit 的 diff 都很短（几行），但每个都堵住了一类内存安全或可用性漏洞；且 `njs_buffer.c` 与 `qjs_buffer.c` 两份都改（案例②）或只改受影响一侧（案例①③④）。
5. **预期结果**：你能用一句话说清每个修复「原来错在哪、为什么是这一类边界条件」。
6. 运行回归测试（**待本地验证**，需先构建）：`NJS_PATH=test/js ./build/njs test/buffer.t.js`，应全绿（包含 `concatRevalidate_tsuite` 等新用例）。

> 本实践的核心是**阅读 diff + 定位源码**，不需要构建即可完成；运行测试那一步标注为「待本地验证」。

#### 4.3.6 小练习与答案

**练习 1**：案例②里，为什么 `byteLength == 0` 能通过原本的边界检查 `size + index > byte_length`？

**参考答案**：因为此时 `size` 取自 `byteLength`，是 0，所以 `size + index` 退化为 `index`，只要 `index <= byte_length` 就通过——边界检查形同虚设。随后 `switch(size)` 落到默认的 6 字节分支，于是越界。修复把校验改成 `size == 0 || size > 6`，从源头堵住。

**练习 2**：案例①的 TOCTOU 为什么在内置引擎存在、在 QuickJS 侧不存在？

**参考答案**：内置引擎的 `concat` 把「校验类型」与「拷贝」拆成两遍，两遍都各自重新取值；而 QuickJS 侧的 `qjs_buffer_concat` 用 `qjs_typed_array_data` 这个「取值即校验」的入口，每次取元素都同时完成「是不是合法 TypedArray + 拿到字节区间」，不存在「校验之后类型还能变」的窗口。

**练习 3**：案例③的死循环，根因是循环步长 `n` 可能为 0。请举出另一种「步长可能为 0」的代码模式。

**参考答案**：任何形如 `while (p < end) { n = min(SRC_LEN, end - p); copy(p, src, n); p += n; }` 的循环，当 `SRC_LEN == 0` 时 `n` 恒为 0，`p` 永不前进，都会死循环。修复要么在循环前特判 `SRC_LEN == 0`，要么保证 `n` 至少为 1（或直接改用 `memset`）。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个综合任务：

**任务**：写一份「Buffer 内存安全自检清单」。要求：

1. 列出 Buffer 在两引擎下的**存储差异**（内置引擎：自定义 `njs_typed_array_t` + `njs_array_buffer_t`；QuickJS：QuickJS 自管 + njs 贴 Buffer 类）。
2. 对本讲的四类边界条件（TOCTOU、边界值遗漏、零长度循环、视图偏移忽略），各给出：
   - 一句「如何在写代码时预防」的规则（例如：「取值与校验应在同一次操作中完成，避免跨调用持有未校验的指针」）。
   - 一个本讲引用过的源码位置作为佐证。
3. 选一个修复（推荐案例②，最短），在不改动源码的前提下，**写一段最小 JS** 来触发修复前的危险行为（例如对一个 1 字节的 Buffer 调用 `readUIntLE(offset, 0)`），并预测修复后应抛什么错误（`RangeError`/`TypeError` 及其信息）。运行步骤标注「待本地验证」。

**验收标准**：你的清单里，每条规则都能对应到本讲列出的某个 `src/*.c` 行号；你的最小 JS 用例的错误信息，与 [src/qjs_buffer.c:1181-1184](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs_buffer.c#L1181-L1184) 或 [src/njs_buffer.c:1026-1028](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_buffer.c#L1026-L1028) 里抛出的字符串一致（`"byteLength" must be >= 1 and <= 6`）。

## 6. 本讲小结

- 在内置引擎里，**Buffer 就是换了原型链的 `Uint8Array`**：同一个 `njs_typed_array_t`（`type = UINT8_ARRAY`），`njs_buffer_alloc` 只是把 `__proto__` 改成 Buffer 原型。
- 二进制数据是「后端 `njs_array_buffer_t`（字节）+ 视图 `njs_typed_array_t`（offset/byte_length/type）」两层结构；视图的真实字节起点是 `offset × element_size`。
- QuickJS 侧不重造 TypedArray，而是用 `JS_NewClass(QJS_CORE_CLASS_ID_BUFFER)` + 让 Buffer 原型继承 Uint8Array 原型；取字节用共享工具 `qjs_typed_array_data`（「取值即校验」）。
- 二进制操作有四类典型边界陷阱，对应四个真实修复：`concat()` 的 **TOCTOU**（943a9f35）、`readInt/writeInt` 的**边界值遗漏**（e0712408）、`fill` 的**零长度死循环**（8b0a1a87）、同类型拷贝的**视图偏移忽略**（210bd6b1）。
- 上述修复体现了双引擎铁律：受影响的两侧（如 `njs_buffer.c` + `qjs_buffer.c`）必须同步修改，且都配有 test262 风格回归用例（`test/buffer.t.js`）。

## 7. 下一步学习建议

- **横向对比**：本讲只看了 `Buffer`/`TypedArray`。建议接着读 `src/njs_string.c`（u5-l2 已涉及），对比「字节容器」与「字符串容器」在内部表示上的异同，理解为什么 `Buffer.prototype.toString` 需要编码参数。
- **纵向深入 QuickJS**：本讲提到 `JS_GetTypedArrayBuffer`/`JS_GetArrayBuffer`。若你想理解 QuickJS 自己是怎么管理 ArrayBuffer 生命周期的，可去上游 QuickJS 源码（`quickjs.c`）读 `JS_FreeArrayBuffer` 与 TypedArray 的 finalizer。
- **测试体系**：本讲的回归用例都在 `test/buffer.t.js`。建议学 u10-l1（测试体系）后，尝试自己照 `concatRevalidate_tsuite` 的写法，为案例④（视图偏移）补一个 test262 风格用例，跑 `make test262` 验证。
- **安全视角**：四个修复里有三个是内存安全（越界读/写、野指针）。若你对这类 bug 感兴趣，可学 u10-l3（AddressSanitizer 构建），用 `--address-sanitizer=YES` 重新构建，复现修复前的越界并观察 ASan 报告。
