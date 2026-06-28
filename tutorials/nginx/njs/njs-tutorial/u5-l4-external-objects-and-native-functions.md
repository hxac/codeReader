# 外部对象与原生函数：扩展 njs 的 C 接口

## 1. 本讲目标

本讲回答一个关键问题：**njs 自己定义的那些「宿主对象」（`console`、`r`、`s`、`ngx`……）是怎么从 C 代码里造出来的？** 如果你想把 njs 嵌进自己的 C 程序、暴露一组自定义对象给 JS 用，本讲就是入口。

学完本讲你应当能够：

- 读懂 `njs_external_t` 描述符，知道它如何用一张静态表声明一个宿主对象的「形状」（有哪些方法、哪些属性、哪些子对象）。
- 说清 `njs_vm_external_prototype`（注册原型）→ `njs_vm_external_create`（创建实例）→ `njs_vm_external`（取回 C 指针）三件套的协作。
- 理解 `njs_prop_handler_t` 这一回调在 GET / SET / DELETE 三种上下文下的入参约定与返回码约定。
- 区分「外部对象（external object）」与「原生函数（native function）」两条不同的扩展路径，并知道何时用 `njs_vm_function_alloc`。

本讲只覆盖**内置 njs 引擎**一侧的 C 扩展机制（QuickJS 侧另有 `qjs_*` 包装，见 [u6-l1](u6-l1-quickjs-wrapper.md)）。

## 2. 前置知识

阅读本讲前，你应当已经建立以下认知（来自前置讲义）：

- **值的内部表示**（[u2-l2](u2-l2-value-representation.md)）：`njs_value_t` 是 16 字节标签联合体，类型藏在头部，对象值在 payload 里存堆指针。
- **对象模型**（[u5-l1](u5-l1-object-model-and-properties.md)，本讲的直接依赖）：`njs_object_t` 同时持「只读共享哈希 `shared_hash`」与「实例私有可变哈希 `hash`」两张表；属性查找沿原型链 `__proto__` 回溯；首次写共享属性会触发 `njs_prop_private_copy` 写时复制。本讲的「外部对象」几乎就是这个机制的特化版——只是它的 `shared_hash` 不是内建方法表，而是 C 代码用 `njs_external_t` 描述出来的。
- **函数调用**（[u4-l3](u4-l3-function-call-frames.md)）：`njs_function_t` 用 `native` 位区分「C 原生函数」与「字节码 lambda」。

两个本讲会反复出现的术语：

- **宿主对象（host object）/ 外部对象（external object）**：由 C 代码（而非 JS 字面量或内建构造器）创建的对象，其背后通常绑定一个 C 结构体指针。
- **原型 id（proto_id）**：注册一张 `njs_external_t` 表后，njs 返回一个整数句柄，后续用它「按图施工」创建实例。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `src/njs.h` | 公共头。定义 `njs_external_t` 描述符、`njs_prop_handler_t` / `njs_function_native_t` 回调签名，以及 `njs_vm_external_prototype` 等导出 API。 |
| `src/njs_extern.c` | 外部对象机制的全部实现：把描述符表编译成「槽位数组」、创建实例、取回 C 指针、内置属性处理器。 |
| `src/njs_function.c` | 原生函数的分配器 `njs_vm_function_alloc`。 |
| `src/njs_value.h` | `njs_exotic_slots_t` 槽位结构、`njs_object_t` 上的 `slots` 字段。 |
| `external/njs_shell.c` | CLI 中 `console` 对象的完整声明与注册——本讲最主要的真实范例。 |
| `nginx/ngx_js_shared_dict.c` | `ngx.shared` 字典的属性处理器范例（GET 取宿主字段）。 |

## 4. 核心概念与源码讲解

### 4.1 external 描述符：用 C 声明一个宿主对象的形状

#### 4.1.1 概念说明

JS 里我们写 `console.log(...)`，这个 `console` 对象并不是用 JS 代码 `new Object()` 造出来的，而是 CLI（或 NGINX 模块）在启动时用 C 代码「声明 + 物化」出来的。njs 提供了一种**声明式**的做法：你用一张静态的 `njs_external_t` 数组描述「这个对象有哪些成员」，njs 负责把它编译成引擎内部的结构并挂到原型上。

这种做法的好处是：**形状在编译期就固定**，运行期只是查表，避免了为每个宿主对象动态拼装哈希表的开销；同时它与 u5-l1 讲的「共享哈希」天然契合——一张描述符表对应一张只读 `shared_hash`，被所有实例复用。

#### 4.1.2 核心流程

一个描述符项的形状由三部分决定：

1. **`flags` 的低 2 位**决定它是哪一类成员：属性（PROPERTY）、方法（METHOD）、子对象（OBJECT），或「自身元信息」（SELF）。
2. **`name`** 是成员名，可以是字符串，也可以是已知 symbol（如 `Symbol.toStringTag`）。
3. **`u` 联合**按类别存放具体内容：属性放 `handler`/`value`，方法放 C 函数指针 `native`，子对象放一张嵌套的 `njs_external_t` 表。

物化时，`flags & NJS_EXTERN_TYPE_MASK`（低 2 位）做一次 `switch` 分流到三条装配路径。

#### 4.1.3 源码精读

描述符结构本身定义在公共头里：

[src/njs.h:L161-L181](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs.h#L161-L181) —— 成员类别枚举与属性子类型枚举。注意 `NJS_EXTERN_TYPE_MASK = 3`，所以低 2 位才是类别，第 3 位 `NJS_EXTERN_SYMBOL = 4` 是「名字是 symbol」的附加标志。

[src/njs.h:L184-L222](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs.h#L184-L222) —— `struct njs_external_s` 的完整定义。它是一个带 `u` 联合的描述符：`property` 子结构带 `handler`/`magic16`/`magic32`，`method` 子结构带 `native`/`magic8`/`ctor`，`object` 子结构带一张嵌套的 `properties` 表和它自己的 `prop_handler`/`keys`。

> 小贴士：`magic8` / `magic16` / `magic32` 是「随成员一起存的几个魔法数」，相当于给回调函数附带的参数。后面会看到，多个 JS 方法可以共享同一个 C 实现，靠 `magic8` 区分到底要做哪件事（典型例子：`console.log/info/warn/error` 共用 `njs_ext_console_log`）。

装配分流发生在 `njs_external_add` 内部：

[src/njs_extern.c:L90-L172](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_extern.c#L90-L172) —— 按 `flags & NJS_EXTERN_TYPE_MASK` 把每个描述符项装配进一张 `njs_flathsh_t`：

- `NJS_EXTERN_METHOD` 分支（[L91-L108](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_extern.c#L91-L108)）：分配一个 `njs_function_t`，置 `native = 1`，把 `external->u.method.native`（C 函数指针）和 `magic8`、`ctor` 塞进去，再用 `njs_set_function` 包成属性值。
- `NJS_EXTERN_PROPERTY` 分支（[L110-L129](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_extern.c#L110-L129)）：如果给了 `handler` 就做成 `NJS_PROPERTY_HANDLER`（动态属性），否则把 `value` 当成静态字符串塞进去。
- `NJS_EXTERN_OBJECT` 分支（[L131-L172](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_extern.c#L131-L172)）：递归处理嵌套表，给子对象也分配一个槽位，并用一个内置处理器 `njs_external_prop_handler` 把「访问子对象」也变成惰性物化。

真实范例：CLI 的 `console` 对象就是一张 `njs_external_t` 表：

[external/njs_shell.c:L270-L364](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L270-L364) —— `njs_ext_console[]` 声明了 `dump`/`error`/`info`/`log`/`time`/`timeEnd`/`warn` 等方法（全是 `NJS_EXTERN_METHOD`），以及一项 `Symbol.toStringTag`（`NJS_EXTERN_PROPERTY | NJS_EXTERN_SYMBOL`，静态值 `"Console"`）。注意 `log`/`info`/`warn`/`error` 四个方法共享同一个 `native = njs_ext_console_log`，只靠 `magic8` 区分日志级别。

#### 4.1.4 代码实践

**实践目标**：在真实源码里验证「描述符 → 成员」的映射。

**操作步骤**：

1. 打开 [external/njs_shell.c:L270-L364](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L270-L364)，对照 `njs_ext_console[]` 表。
2. 构建并运行 CLI（若尚未构建，参考 [u1-l3](u1-l3-build-and-run-cli.md)）：

   ```bash
   ./build/njs -e 'console.log(Object.prototype.toString.call(console))'
   ```

3. 再运行：

   ```bash
   ./build/njs -e 'console.log(typeof console.log, typeof console.time, typeof console.notExist)'
   ```

**需要观察的现象**：

- 第 2 步应输出 `[object Console]`——这正是描述符里 `Symbol.toStringTag = "Console"`（[L322-L328](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L322-L328)）的效果。
- 第 3 步应输出 `function function undefined`，说明 `log`/`time` 是 C 方法、`notExist` 不在表里。

**预期结果**：能从描述符表反推出 JS 侧可见的成员集合。若未构建 CLI，则上述运行结果为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`njs_ext_console[]` 里 `log` 和 `error` 共享同一个 C 函数 `njs_ext_console_log`，引擎靠什么区分它们？
**答案**：靠描述符里的 `magic8` 字段。`log` 的 `magic8 = NJS_LOG_INFO`，`error` 的 `magic8 = NJS_LOG_ERROR`（见 [external/njs_shell.c:L286-L319](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L286-L319)）。运行时 `njs_ext_console_log` 从 `magic` 参数里读出级别（[L3722](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L3722)）。

**练习 2**：描述符 `flags` 的低 2 位与 `NJS_EXTERN_SYMBOL`（值 4）为什么能并存而不冲突？
**答案**：低 2 位（掩码 `NJS_EXTERN_TYPE_MASK = 3`）编码类别，`NJS_EXTERN_SYMBOL` 是第 3 位（bit 2），二者占不同的位，可按位组合，分别用 `flags & 3` 与 `flags & NJS_EXTERN_SYMBOL` 读取。

---

### 4.2 注册与物化：从描述符到外部实例

#### 4.2.1 概念说明

光有描述符表还不够——它只是 C 代码里的静态数据。要让它变成 JS 里能 `console.log` 的对象，需要两步：

1. **注册原型**：`njs_vm_external_prototype(vm, 表, 项数)` 把描述符表编译成引擎内部的「槽位数组（slots）」，返回一个整数 `proto_id`。
2. **创建实例**：`njs_vm_external_create(vm, &value, proto_id, c_ptr, shared)` 按 `proto_id` 「按图施工」，造出一个对象，并把你的 C 指针 `c_ptr` 绑进去。

之后在 C 回调里，用 `njs_vm_external(vm, proto_id, value)` 反向取回这个 C 指针。这就是「JS 对象 ↔ C 结构体」的桥。

#### 4.2.2 核心流程

注册与物化的协作如下（伪代码）：

```
# 配置期（VM 启动时，只做一次）
proto_id = njs_vm_external_prototype(vm, njs_ext_console, nitems(njs_ext_console))
#   └─ njs_external_protos(): 数一数递归嵌套需要几个 slot
#   └─ njs_arr_create(): 分配 slots 数组
#   └─ njs_external_add(): 把表编译进每个 slot 的 external_shared_hash

# 创建实例（每个 console 对象一次）
njs_vm_external_create(vm, &value, proto_id, console_ptr, shared=0)
#   └─ 取出 proto_id 对应的 slots
#   └─ 分配一个 object_value，挂上 slots 的 shared_hash
#   └─ 把 console_ptr 用 njs_make_tag(proto_id) 打标签后存进 value 字段

# C 回调里取回指针
console = njs_vm_external(vm, proto_id, args[0])
#   └─ 校验 value 的 tag == njs_make_tag(proto_id)，匹配才返回指针
```

**类型判别的小技巧**：C 指针被打上 `njs_make_tag(proto_id)` 标签存进对象值里（见 [src/njs_extern.c:L407](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_extern.c#L407)）。取回时 `njs_vm_external` 用 `njs_is_object_data(value, njs_make_tag(proto_id))` 校验标签——这样它就能确认「这个值确实是我这个 proto_id 造出来的外部对象」，避免把别家的外部指针误当自家的用。

#### 4.2.3 源码精读

**注册原型**：先数 slot 数，再分配，再编译。

[src/njs_extern.c:L249-L267](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_extern.c#L249-L267) —— `njs_external_protos` 递归统计嵌套子对象需要多少个 slot（每张表至少占 1 个，每个 `NJS_EXTERN_OBJECT` 子项再递归累加）。

[src/njs_extern.c:L270-L307](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_extern.c#L270-L307) —— `njs_vm_external_prototype`：分配 `protos` 数组，调 `njs_external_add` 编译，把数组登记进 `vm->protos`，最后返回下标 `vm->protos->items - 1` 作为 `proto_id`。

> 关键认知：`proto_id` 就是 `vm->protos`（一个 `njs_arr_t **` 数组）的下标。同一张描述符表注册两次会得到两个不同的 id。

**创建实例**：

[src/njs_extern.c:L382-L410](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_extern.c#L382-L410) —— `njs_vm_external_create`：先校验 `proto_id` 合法（[L390-L392](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_extern.c#L390-L392)），分配一个 `njs_object_value_t`，把对应 slot 的 `external_shared_hash` 挂到对象的 `shared_hash`、把 `slots` 挂到 `object.slots`（[L399-L404](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_extern.c#L399-L404)），最后用 `njs_set_data(&ov->value, external, njs_make_tag(proto_id))` 把 C 指针打标签存好（[L407](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_extern.c#L407)）。`shared` 参数控制对象是否标记为共享（共享对象在克隆 VM 时行为不同，参考 u2-l1 的克隆模型）。

**取回指针**：

[src/njs_extern.c:L413-L428](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_extern.c#L413-L428) —— `njs_vm_external`：校验标签匹配后返回 `njs_object_data(value)`；若指针本身是 `NULL`，则退回 `vm->external`（这是 VM 级别的「全局外部指针」，由 `njs_vm_clone` 的 `external` 参数注入，见 [u2-l1](u2-l1-vm-lifecycle-api.md)）。

**真实范例**：CLI 注册并创建 `console` 的完整过程：

[external/njs_shell.c:L952-L968](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L952-L968) —— `njs_console_proto_id = njs_vm_external_prototype(vm, njs_ext_console, njs_nitems(njs_ext_console))` 注册原型；接着 `njs_vm_external_create(vm, &value, njs_console_proto_id, console, 0)` 创建实例（`console` 是一个 `njs_console_t *` C 指针）；最后 `njs_vm_bind(vm, &console_name, &value, 0)` 把它绑到全局变量 `console` 上。

#### 4.2.4 代码实践

**实践目标**：跟踪一次外部实例的创建，确认 C 指针被正确桥接。

**操作步骤**：

1. 阅读 [external/njs_shell.c:L952-L968](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L952-L968)，画出 `njs_vm_external_prototype` → `njs_vm_external_create` → `njs_vm_bind` 的三步时序。
2. 跟进 [external/njs_shell.c:L3743-L3786](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L3743-L3786)，看 `console.time()` 的 C 实现 `njs_ext_console_time` 如何在第 3754 行用 `njs_vm_external(vm, njs_console_proto_id, njs_argument(args, 0))` 取回那个 `njs_console_t *`。

**需要观察的现象**：`args[0]` 是 JS 侧的 `this`（即 `console` 对象本身）。C 代码并不直接持有 `console` 的 C 结构体，而是通过 `njs_vm_external` 从 JS 值里「反查」回来。

**预期结果**：能说清「C 指针 → 打标签存进对象 → JS 调用方法 → 回调里反查回同一指针」的闭环。运行结果无需本地验证（纯源码阅读型实践）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `njs_vm_external` 要校验 `njs_make_tag(proto_id)`，而不是直接返回 `njs_object_data(value)`？
**答案**：因为可能存在多种不同的外部对象（`console`、`r`、`s`、`ngx.shared`……），每个都有自己的 `proto_id` 和背后的 C 结构体类型。校验标签能确保「这个值确实是按我这个 proto_id 造出来的」，类型不匹配时返回 `NULL`，避免把一种结构体指针误当成另一种解引用（会段错误）。

**练习 2**：如果两次调用 `njs_vm_external_prototype(vm, 同一张表, 同样项数)`，会得到相同的 `proto_id` 吗？
**答案**：不会。每次调用都会在 `vm->protos` 末尾追加一个新数组并返回新下标（[src/njs_extern.c:L299-L306](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_extern.c#L299-L306)），所以 `proto_id` 每次不同。

---

### 4.3 属性处理器 prop_handler：GET / SET / DELETE 三合一

#### 4.3.1 概念说明

4.1 讲的 `NJS_EXTERN_METHOD` 是「方法」，那 `NJS_EXTERN_PROPERTY` 带 `handler` 的就是「动态属性」——它的值不是固定的，而是每次访问时由 C 函数现算。这非常适合暴露那些「值在 C 侧随时变化」的字段，比如 `ngx.shared.dict.name`（zone 的名字）、`r.uri`（当前请求 URI）。

njs 用**同一个回调类型 `njs_prop_handler_t`** 同时承担 getter、setter、delete 三种职责，靠入参的组合来区分上下文。这是一种紧凑的设计：一个属性只需注册一个 C 函数。

#### 4.3.2 核心流程

`njs_prop_handler_t` 的签名是：

```c
njs_int_t (*njs_prop_handler_t)(njs_vm_t *vm, njs_object_prop_t *prop,
    uint32_t atom_id, njs_value_t *value, njs_value_t *setval,
    njs_value_t *retval);
```

上下文判别规则（来自公共头注释）：

| `retval` | `setval` | 上下文 | 你应当做什么 |
|---|---|---|---|
| 非 NULL | NULL | **GET** | 把属性值写进 `retval` |
| 非 NULL | 非 NULL | **SET** | 把 `*setval` 写回宿主，`retval` 通常也置为新值 |
| NULL | — | **DELETE** | 删除宿主侧对应字段 |

入参含义：

- `prop`：属性描述符本身，可用 `njs_vm_prop_magic16(prop)` / `njs_vm_prop_magic32(prop)` 读出注册时塞进去的魔法数（见 4.1.3）。
- `atom_id`：属性名的 atom（整数形式的名字，见 [u2-l4](u2-l4-atom-table.md)）。
- `value`：宿主对象（即 `this`），用 `njs_vm_external(vm, proto_id, value)` 取回 C 指针。
- `setval`：SET 时的新值；GET/DELETE 时为 NULL。
- `retval`：GET/SET 的输出；DELETE 时为 NULL（这正是 DELETE 的判别信号）。

返回码三选一：`NJS_OK`（成功）、`NJS_DECLINED`（对象不适用，`retval` 置 undefined）、`NJS_ERROR`（出错，用 `njs_vm_exception_get` 取异常）。

#### 4.3.3 源码精读

约定写在公共头里：

[src/njs.h:L100-L115](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs.h#L100-L115) —— `njs_prop_handler_t` 的注释与 typedef。这段注释是理解整个机制最权威的依据，务必精读。

njs 还内置了一个通用的属性处理器 `njs_external_property`，它示范了「用 `magic16` 当字段类型、`magic32` 当字节偏移」的 GET 写法：

[src/njs_extern.c:L442-L475](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_extern.c#L442-L475) —— 先 `njs_vm_external` 取回 C 指针 `p`（[L450](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_extern.c#L450)），再按 `magic16` 区分 `NJS_EXTERN_TYPE_INT`/`UINT`/`VALUE`，用 `magic32` 当偏移从结构体里读出字段（[L457-L472](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_extern.c#L457-L472)）。这是一个纯 GET 实现：它没处理 `setval`，所以这种属性默认只读。

> 这正是 `magic16` / `magic32` 的妙用：同一个 C 处理器，配不同的魔法数，就能读不同类型、不同偏移的字段，无需为每个属性写一个函数。

真实范例（NGINX 侧）：`ngx.shared.dict.name` 的 getter：

[nginx/ngx_js_shared_dict.c:L1095-L1109](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_shared_dict.c#L1095-L1109) —— `njs_js_ext_shared_dict_name`：先 `njs_vm_external` 取回 `ngx_shm_zone_t *`，取不回就 `retval` 置 undefined 并返回 `NJS_DECLINED`（[L1102-L1105](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_shared_dict.c#L1102-L1105)），否则把 zone 名字字符串写进 `retval`。这也是纯 GET（没碰 `setval`），所以 `dict.name` 是只读的。

子对象的惰性物化处理器：

[src/njs_extern.c:L187-L246](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_extern.c#L187-L246) —— `njs_external_prop_handler` 处理「访问一个 `NJS_EXTERN_OBJECT` 子成员」的情形：GET 时（`setval == NULL`）它不是返回固定值，而是临时分配一个 `object_value`，把子 slot 的 `shared_hash` 挂上、把同一个 C 指针绑进去（[L208-L223](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_extern.c#L208-L223)），并把这个结果缓存进对象的私有哈希（[L230-L243](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_extern.c#L230-L243)）——下次访问就直接命中，不必再造。这与 u5-l1 讲的「写时复制 / 缓存到本地哈希」是同一思路。

#### 4.3.4 代码实践

**实践目标**：用一个 GET 属性处理器，让 JS 读到 C 侧的一个整数字段。

**操作步骤**：阅读下面这段「示例代码」（非项目原有代码），它模拟一个计数器宿主对象：

```c
/* 示例代码：仅为说明 prop_handler 三上下文约定，非仓库内文件 */

typedef struct {
    njs_int_t  count;   /* 偏移 0 */
} my_counter_t;

/* GET: retval!=NULL, setval==NULL
 * SET: retval!=NULL, setval!=NULL
 * DELETE: retval==NULL */
static njs_int_t
my_counter_count_handler(njs_vm_t *vm, njs_object_prop_t *prop,
    uint32_t atom_id, njs_value_t *value, njs_value_t *setval,
    njs_value_t *retval)
{
    my_counter_t  *c;

    c = njs_vm_external(vm, my_counter_proto_id, value);
    if (c == NULL) {
        njs_value_undefined_set(retval);   /* DELETE 时 retval 是 NULL，注意判空 */
        return NJS_DECLINED;
    }

    if (retval == NULL) {
        /* DELETE 上下文：本属性不允许删除 */
        return NJS_DECLINED;
    }

    if (setval != NULL) {
        /* SET 上下文：把新值写回 C 结构体 */
        c->count = (njs_int_t) njs_value_number(setval);
        njs_value_number_set(retval, c->count);
        return NJS_OK;
    }

    /* GET 上下文：把 C 字段读出来 */
    njs_value_number_set(retval, c->count);
    return NJS_OK;
}
```

**需要观察的现象**：

- GET 时三个指针的关系：`value` 是宿主对象（用来取 `c`），`retval` 是输出槽，`setval` 为 NULL。
- SET 时 `setval` 指向 JS 侧赋过来的新值，需把它转成 C 类型写回。
- DELETE 时 `retval == NULL`——这正是判别信号，必须先判空再解引用，否则段错误。

**预期结果**：能说清三个上下文下入参的差异。由于本示例未真正接入构建，运行结果为「待本地验证」；可对照 [nginx/ngx_js_shared_dict.c:L1095-L1109](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_shared_dict.c#L1095-L1109) 验证纯 GET 写法与本项目一致。

#### 4.3.5 小练习与答案

**练习 1**：一个 `prop_handler` 同时承担 GET/SET/DELETE，引擎怎么知道当前该执行哪一个？
**答案**：靠 `retval` 与 `setval` 是否为 NULL 的组合：`retval!=NULL && setval==NULL` 是 GET；`retval!=NULL && setval!=NULL` 是 SET；`retval==NULL` 是 DELETE（见 [src/njs.h:L101-L105](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs.h#L101-L105)）。

**练习 2**：为什么 `njs_external_property`（[src/njs_extern.c:L442-L475](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_extern.c#L442-L475)）注册出来的属性是只读的？
**答案**：因为它只处理了 GET 分支（直接读 `magic32` 偏移处的字段写进 `retval`），完全没检查也没写 `setval`。当引擎以 SET 上下文调用它时，它依然走 GET 逻辑，不会把新值写回，因此表现为只读。

---

### 4.4 原生函数 njs_function_native_t 与 njs_vm_function_alloc

#### 4.4.1 概念说明

外部对象讲的是「对象 + 属性/方法」。但有时你不需要一整个宿主对象，只想往全局塞一个**独立的 C 函数**（比如 `setTimeout`、`print`）。这时用「原生函数（native function）」更轻量。

原生函数就是用 C 写的、可被 JS 当函数调用的实体。在 njs 内部它复用 `njs_function_t` 结构——还记得 [u4-l3](u4-l3-function-call-frames.md) 讲的吗：同一个 `njs_function_t` 靠 `native` 位区分，为 0 是字节码 lambda，为 1 是 C 原生函数（`u.native` 存函数指针）。调用时引擎直接执行 C 函数，不进解释器循环。

#### 4.4.2 核心流程

原生函数的签名是：

```c
typedef njs_int_t (*njs_function_native_t)(njs_vm_t *vm, njs_value_t *args,
    njs_uint_t nargs, njs_index_t magic8, njs_value_t *retval);
```

- `args` / `nargs`：实参。`args[0]` 是 `this`，`args[1]` 起是真正传入的参数（用 `njs_arg(args, nargs, i)` 安全取，越界返回 undefined，见 [u2-l2](u2-l2-value-representation.md)）。
- `magic8`：注册时塞进去的魔法数（就是描述符里的 `method.magic8`），用于「一个 C 实现、多个 JS 方法」的分流。
- `retval`：返回值输出槽。返回 `NJS_OK` 表示成功，`NJS_ERROR` 表示抛了异常。

分配一个原生函数有两种入口：

1. **作为外部对象的方法**：在 `njs_external_t` 里写 `NJS_EXTERN_METHOD`，`njs_external_add` 会自动分配 `njs_function_t`（4.1.3 已讲）。
2. **作为独立全局函数**：直接调 `njs_vm_function_alloc(vm, native, shared, ctor)` 拿到一个 `njs_function_t *`，再用 `njs_function_bind` 之类的方式绑到全局名上。

#### 4.4.3 源码精读

签名定义：

[src/njs.h:L118-L119](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs.h#L118-L119) —— `njs_function_native_t` 的 typedef。

分配器实现：

[src/njs_function.c:L70-L91](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_function.c#L70-L91) —— `njs_vm_function_alloc`：从内存池零分配一个 `njs_function_t`，置 `native = 1`、`ctor = ctor`、`shared = shared`，把 `native` 函数指针存进 `u.native`，并接好 `function_instance_hash` 与 `Function` 原型链。注意它和 `njs_external_add` 里 METHOD 分支（[src/njs_extern.c:L92-L106](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_extern.c#L92-L106)）做的事几乎一样，只是入口不同：前者是「单独造一个函数」，后者是「在装配描述符表时顺带造」。

导出声明：

[src/njs.h:L394-L395](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs.h#L394-L395) —— `njs_vm_function_alloc` 的原型。

真实范例（`console.log` 的 C 实现）：

[external/njs_shell.c:L3714-L3740](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L3714-L3740) —— `njs_ext_console_log`：从 `magic & NJS_LOG_MASK` 读出日志级别（[L3722](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L3722)），遍历 `args[1..]` 逐个 `njs_vm_value_dump` 转字符串后输出（[L3724-L3735](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L3724-L3735)），最后 `retval` 置 undefined 返回 `NJS_OK`。这就是「`log`/`info`/`warn`/`error` 四个方法共用一个 C 函数、靠 `magic8` 分流」的落地。

#### 4.4.4 代码实践

**实践目标**：体会 `magic8` 分流的威力——一个 C 函数撑起多个 JS 方法。

**操作步骤**：

1. 运行 CLI（已构建）：

   ```bash
   ./build/njs -e 'console.log("a"); console.info("b"); console.warn("c"); console.error("d")'
   ```

2. 对照 [external/njs_shell.c:L3714-L3740](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L3714-L3740)，确认这四次调用进入的是**同一个** C 函数 `njs_ext_console_log`，只是 `magic8` 不同。

**需要观察的现象**：四条消息都会打印，但级别不同（在 NGINX 集成下会落到不同日志级别；纯 CLI 下都走 stderr/stdout）。

**预期结果**：能解释「JS 侧 4 个方法 → C 侧 1 个函数 + 4 个 magic8」的映射。若未构建 CLI，运行结果为「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：原生函数和字节码函数在 `njs_function_t` 里如何区分？调用时引擎行为有何不同？
**答案**：靠 `native` 位区分。`native == 1` 时 `u.native` 是 C 函数指针，调用时引擎直接执行该 C 函数、自行处理返回；`native == 0` 时 `u.lambda` 指向字节码产物，调用时建新帧并递归进入解释器（详见 [u4-l3](u4-l3-function-call-frames.md)）。

**练习 2**：`njs_vm_function_alloc` 与 `njs_external_add` 的 METHOD 分支都造 `njs_function_t`，二者何时选用？
**答案**：`njs_external_add` 用于「方法属于某个外部对象」的场景（在描述符表里声明，随原型一起装配）；`njs_vm_function_alloc` 用于「独立的、不属于任何外部对象的全局函数」（如 `setTimeout`），造完后还要手动绑到某个全局名上。

---

## 5. 综合实践

把本讲四个模块串起来，设计一个最小的 C 扩展（**示例代码，非仓库内文件，待本地验证**）：声明一个「计数器」外部原型，带一个方法 `inc()` 和一个动态属性 `count`，并写好注册与创建代码。

**第 1 步：声明描述符表**

```c
/* 示例代码 */
static const njs_external_t  my_ext_counter[] = {

    {
        .flags = NJS_EXTERN_METHOD,
        .name.string = njs_str("inc"),
        .writable = 1, .configurable = 1, .enumerable = 1,
        .u.method = {
            .native = my_counter_inc,     /* njs_function_native_t */
        },
    },

    {
        .flags = NJS_EXTERN_PROPERTY,
        .name.string = njs_str("count"),
        .writable = 0, .configurable = 0, .enumerable = 1,
        .u.property = {
            .handler = my_counter_count_handler,  /* njs_prop_handler_t */
            .magic16 = NJS_EXTERN_TYPE_INT,
            .magic32 = offsetof(my_counter_t, count),
        },
    },
};
```

注意 `count` 属性复用了 njs 内置处理器 `njs_external_property`（[src/njs_extern.c:L442-L475](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_extern.c#L442-L475)），用 `magic16 = NJS_EXTERN_TYPE_INT`、`magic32 = 字段偏移`，这样**完全不用自己写 GET 逻辑**。

**第 2 步：注册原型并创建实例**

```c
/* 示例代码 */
my_counter_proto_id = njs_vm_external_prototype(vm, my_ext_counter,
                                                njs_nitems(my_ext_counter));
/* ... */
njs_vm_external_create(vm, &value, my_counter_proto_id, &my_counter_state, 0);
njs_vm_bind(vm, &njs_str("counter"), &value, 0);
```

**第 3 步：实现 `inc` 方法**

```c
/* 示例代码 */
static njs_int_t
my_counter_inc(njs_vm_t *vm, njs_value_t *args, njs_uint_t nargs,
               njs_index_t magic, njs_value_t *retval)
{
    my_counter_t  *c;
    c = njs_vm_external(vm, my_counter_proto_id, njs_argument(args, 0));
    if (c == NULL) {
        njs_vm_type_error(vm, "external value is expected");
        return NJS_ERROR;
    }
    c->count++;
    njs_value_number_set(retval, c->count);
    return NJS_OK;
}
```

**验证目标**：在 JS 里写 `counter.inc(); counter.inc(); console.log(counter.count)`，应当打印 `2`。

**思考题（结合本讲）**：

1. 为什么 `inc()` 里取 `this` 要用 `njs_vm_external(vm, proto_id, args[0])` 而不是直接解引用？（答：要先校验类型标签，见 4.2.3。）
2. `counter.count` 为什么是只读的？（答：因为复用的 `njs_external_property` 只实现了 GET，见 4.3.5 练习 2。）
3. 如果想让 `count` 可写，该怎么办？（答：自己写一个 `njs_prop_handler_t`，在 `setval != NULL` 分支里把新值写回 C 结构体，见 4.3.4 示例。）

> 说明：本综合实践需要把示例代码接入 njs 的嵌入式构建（参考 [u2-l1](u2-l1-vm-lifecycle-api.md) 的 `njs_vm_create` 流程）才能真正运行，属于「设计 + 接入」型任务，运行结果待本地验证。

## 6. 本讲小结

- **`njs_external_t` 是声明式描述符**：一张静态表声明一个宿主对象的形状，低 2 位 `flags` 区分属性/方法/子对象/自身，`name` 可为字符串或已知 symbol。
- **三件套桥接 JS 与 C**：`njs_vm_external_prototype` 编译描述符返回 `proto_id`；`njs_vm_external_create` 按 `proto_id` 造实例并把 C 指针打标签存入；`njs_vm_external` 在回调里反查回指针，靠 `njs_make_tag(proto_id)` 做类型判别。
- **`njs_prop_handler_t` 一函三用**：靠 `retval`/`setval` 的 NULL 组合区分 GET / SET / DELETE；`magic16`/`magic32` 当字段类型与偏移的旁路参数，让一个处理器复用于多个属性。
- **原生函数复用 `njs_function_t`**：靠 `native` 位区分 C 函数与字节码 lambda；`njs_vm_function_alloc` 用于独立全局函数，`NJS_EXTERN_METHOD` 用于外部对象的方法；`magic8` 让多个 JS 方法共享一个 C 实现（如 `console.log/info/warn/error`）。
- **与 u5-l1 的关系**：外部对象的 `external_shared_hash` 就是 u5-l1「共享哈希」机制的特化——描述符表编译成只读共享哈希，所有实例复用，首次访问子对象时再惰性物化进私有哈希（`njs_external_prop_handler`）。
- **仅限内置 njs 引擎**：本讲全部机制是内置引擎的；QuickJS 侧另有 `qjs_*` 包装与 `JSValue` 体系，见 [u6-l1](u6-l1-quickjs-wrapper.md)、[u6-l2](u6-l2-dual-engine-module-pattern.md)。

## 7. 下一步学习建议

- **看真实的大型 external 表**：阅读 [external/njs_fs_module.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_fs_module.c) 中的 `njs_ext_stats[]`、`njs_ext_dirent[]`，体会带嵌套子对象与构造器的外部原型如何组织。
- **进入双引擎世界**：本讲的 `njs_external_t` / `njs_vm_external_*` 是内置引擎专属。下一讲 [u6-l1 QuickJS 引擎包装层 qjs.c](u6-l1-quickjs-wrapper.md) 讲 njs 如何在上游 QuickJS 上做等价包装，[u6-l2 双引擎模块模式](u6-l2-dual-engine-module-pattern.md) 讲「一个功能、`njs_*` 与 `qjs_*` 两份实现」的工程约定。
- **回到 NGINX 集成**：等学完 u6，[u8-l1 ngx_js 共享层](u8-l1-ngx-js-shared-layer.md) 会展示 `r`、`s` 这两个最重要的外部对象是如何用本讲的机制注册并挂到请求/会话上的。
