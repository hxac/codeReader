# levels/scope/index：作用域寻址

## 1. 本讲目标

上一讲（u4-l1）我们走通了 `njs_vmcode_interpreter` 的「取指 → 分发 → 执行 → 推进 PC」主循环，并提到每条指令的操作数大多是一个 `njs_index_t`，它指向 `vm->levels` 里某个值槽。本讲就来回答一个具体的运行期问题：**给定一条指令的操作数，njs 到底去哪一块内存里取这个值？**

读完本讲你应该能够：

- 说出 njs 把所有运行期值槽分成 **LOCAL / CLOSURE / GLOBAL / STATIC 四级存储**，以及每一级分别装什么、什么时候被替换。
- 把一个 32 位的 `njs_index_t`（例如反汇编里的 `0123`）拆解成「槽位号 + 存储层级 + 变量类型」三段。
- 解释运行期 `njs_scope_value()` 如何用两次移位定位到 `vm->levels[type][value]`，以及 `let`/`const` 的「暂时性死区（TDZ）」如何在寻址层被守卫。

本讲是 u3-l3（变量与作用域）、u3-l5（字节码格式）、u4-l1（解释器主循环）三讲的交汇点：编译期分配的 `index` 在这里被解码成一次真正的内存访问。

## 2. 前置知识

- **寄存器式 VM**：和「基于栈」的引擎不同，njs 不把中间结果压栈，而是给每个值分配一个编号槽位，指令直接用槽位号做操作数（就像 CPU 用寄存器编号）。`vm->levels` 就是这些槽位的总仓库。
- **njs_value_t**：一个 16 字节的 JS 值（见 u2-l2）。本讲里说的「值槽」就是「一个 `njs_value_t` 的存储位置」。所有槽位里存的都是 `njs_value_t *`（指针，指向真正的值），所以 `levels` 的类型是 `njs_value_t **`。
- **作用域（scope）**：JS 的变量有可见范围。`var`/`function` 属于函数级作用域，`let`/`const` 属于块级作用域。编译期 njs 用 `NJS_SCOPE_GLOBAL / FUNCTION / BLOCK` 三类解析期作用域来组织它们（见 u3-l3）。本讲关心的是这些变量在**运行期**最终落在哪一级存储里。
- **闭包（closure）**：内层函数引用了外层函数的变量时，这些变量不能随外层函数返回而消失，需要被「捕获」。njs 把捕获来的变量单独放在 CLOSURE 级存储。

> 一句话区分两组容易混的词：`NJS_SCOPE_*`（GLOBAL/FUNCTION/BLOCK）是**编译期作用域类型**；`NJS_LEVEL_*`（LOCAL/CLOSURE/GLOBAL/STATIC）是**运行期存储层级**。两者名字里都有 GLOBAL，但属于不同维度，下文会讲清它们的映射关系。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/njs_vm.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.h) | 定义存储层级枚举 `njs_level_type_t`（四级）、解析期作用域枚举 `njs_scope_t`，以及 VM 结构体里的 `levels[NJS_LEVEL_MAX]` 二级指针数组。 |
| [src/njs_scope.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_scope.h) | 本讲的核心头文件：`index` 的位偏移/掩码宏、组装函数 `njs_scope_index`、三段解码函数、以及运行期寻址函数 `njs_scope_value` / `njs_scope_valid_value` / `njs_scope_value_set`。 |
| [src/njs_scope.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_scope.c) | 槽位数组的分配（`njs_scope_make`）、临时槽位分配（`njs_scope_temp_index`）、STATIC 级常量的登记（`njs_scope_global_index`）。 |
| [src/njs_variable.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_variable.h) | 变量类型枚举 `njs_variable_type_t`（CONST/LET/CATCH/VAR/FUNCTION），占用 `index` 的低 4 位。 |
| [src/njs_variable.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_variable.c) | 变量在编译期获得 `index` 的分配点，体现「请求 LOCAL、全局作用域改写为 GLOBAL」的规则。 |
| [src/njs_function.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_function.c) | 函数调用时对 LOCAL / CLOSURE 两级指针的「保存—替换—恢复」。 |
| [src/njs_vm.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c) | GLOBAL 级槽位数组的扩容、克隆 VM 时 GLOBAL 指针的传递。 |

---

## 4.1 四级存储层级：LOCAL / CLOSURE / GLOBAL / STATIC

### 4.1.1 概念说明

njs 把所有运行期值槽按「生命周期 + 可见性」分成四级。为什么不是一个大数组了事？因为不同变量的存活范围差别巨大：

- 当前函数的局部变量和临时变量，函数一返回就该整体作废；
- 闭包捕获的变量，要跟随内层函数存活；
- 全局变量，整个 VM 生命周期都在；
- 字面量常量（如 `42`、`"hello"`），更是跨请求/跨克隆都要复用的「永久值」。

把它们放进四个独立的数组，njs 就能用「换指针」实现函数调用的进出——调用一个函数时，只需把 LOCAL 指针指向新函数的局部数组、把 CLOSURE 指针指向新函数捕获的闭包数组，原函数的局部数组原封不动地留在内存里，返回时再把指针换回来。这比逐个 push/pop 值要快得多，也省去了名字查找。

四级层级在 [src/njs_vm.h:L109-L115](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.h#L109-L115) 定义为枚举 `njs_level_type_t`：

```c
typedef enum {
    NJS_LEVEL_LOCAL = 0,
    NJS_LEVEL_CLOSURE,
    NJS_LEVEL_GLOBAL,
    NJS_LEVEL_STATIC,
    NJS_LEVEL_MAX
} njs_level_type_t;
```

| 层级 | 值 | 装的内容 | 生命周期 / 何时被替换 |
|---|---|---|---|
| `NJS_LEVEL_LOCAL` | 0 | 当前函数的局部变量、参数、临时变量 | 每次函数调用/返回时整体替换（`this` 占 0 号槽） |
| `NJS_LEVEL_CLOSURE` | 1 | 被内层函数捕获的「外层」变量 | 跟随被调用的函数走，调用时替换 |
| `NJS_LEVEL_GLOBAL` | 2 | 全局作用域的变量（`var a`） | 整个 VM 生命周期；可扩容 |
| `NJS_LEVEL_STATIC` | 3 | 字面量常量等「绝对/永久」值 | 跨克隆共享，几乎不变 |

这四个数组在 VM 结构体里收拢成一个二级指针数组，见 [src/njs_vm.h:L123-L124](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.h#L123-L124)：

```c
njs_arr_t                *scope_absolute;       // STATIC 级的动态数组容器
njs_value_t              **levels[NJS_LEVEL_MAX]; // 四级存储的入口
```

即 `vm->levels[0]` 是 LOCAL 数组、`vm->levels[1]` 是 CLOSURE 数组，依此类推。每个元素是 `njs_value_t *`（指向一个值）。

### 4.1.2 核心流程

四级存储在运行期的「换指针」节奏可以画成下面这张表（以一次函数调用 `f()` 为例）：

```
调用 f() 之前：
    vm->levels[LOCAL]   ──► 外层函数的局部数组
    vm->levels[CLOSURE] ──► 外层函数的闭包数组
    vm->levels[GLOBAL]  ──► 全局变量数组（不变）
    vm->levels[STATIC]  ──► 常量池（不变）

进入 f() 的解释器（njs_function_frame_invoke）：
    保存：cur_local   = vm->levels[LOCAL]
         cur_closures = vm->levels[CLOSURE]
    替换：vm->levels[LOCAL]   = vm->top_frame->local      // f 自己的局部数组
         vm->levels[CLOSURE] = njs_function_closures(f)    // f 捕获的闭包
    ── 跑 f 的字节码 ──
    恢复：vm->levels[LOCAL]   = cur_local
         vm->levels[CLOSURE] = cur_closures
```

要点：**只有 LOCAL 和 CLOSURE 会在调用边界上换指针；GLOBAL 和 STATIC 全程不动**。这样设计的好处是：函数体里所有 `LOCAL` 槽位的指令在「换指针」之后天然指向新函数的局部数组，无需修改字节码。

GLOBAL 级虽然不随调用换指针，但它会**扩容**——当编译期发现全局作用域的变量数（`scope->items`）比预分配的多时，会重新分配一个更大的数组并搬运旧值（见下方源码）。STATIC 级则由 `njs_scope_global_index()` 在代码生成期逐个登记常量，把 `scope_absolute` 数组的起始指针挂到 `vm->levels[STATIC]`。

### 4.1.3 源码精读

**函数调用时的 LOCAL/CLOSURE 指针切换** —— [src/njs_function.c:L568-L601](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_function.c#L568-L601)：

```c
/* Store current level. */
cur_local = vm->levels[NJS_LEVEL_LOCAL];
cur_closures = vm->levels[NJS_LEVEL_CLOSURE];

/* Replace current level. */
vm->levels[NJS_LEVEL_LOCAL] = vm->top_frame->local;
vm->levels[NJS_LEVEL_CLOSURE] = njs_function_closures(function);
...
ret = njs_vmcode_interpreter(vm, lambda->start, retval, promise_cap, NULL);

/* Restore current level. */
vm->levels[NJS_LEVEL_LOCAL] = cur_local;
vm->levels[NJS_LEVEL_CLOSURE] = cur_closures;
```

这段就是上一小节流程图的代码实现：先存旧值、再换新值、跑完解释器后恢复。注意它**只动 LOCAL 和 CLOSURE**，GLOBAL/STATIC 全程不碰。

**GLOBAL 级的扩容** —— [src/njs_vm.c:L274-L289](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L274-L289)（节选）：

```c
if (scope->items > global_items) {
    global = vm->levels[NJS_LEVEL_GLOBAL];
    new = njs_scope_make(vm, scope->items);   // 按新大小分配
    ...
    vm->levels[NJS_LEVEL_GLOBAL] = new;        // 指向新数组
    if (global != NULL) {
        while (global_items != 0) {            // 搬运旧值
            global_items--;
            *new++ = *global++;
        }
    ...
```

这段发生在 `njs_vm_compile` 末尾：如果全局作用域需要的槽位比一开始预留的多，就重分配并搬运，保证已有的全局变量不丢。

**STATIC 级常量的登记** —— [src/njs_scope.c:L57-L95](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_scope.c#L57-L95)（关键两行）：

```c
vm->levels[NJS_LEVEL_STATIC] = vm->scope_absolute->start;   // L89
...
*retval = njs_scope_index(NJS_SCOPE_GLOBAL, index, NJS_LEVEL_STATIC,
                          NJS_VARIABLE_VAR);                  // L91
```

每当代码生成期需要一个常量（比如字面量 `42`），就把它追加进 `scope_absolute` 数组，并返回一个 STATIC 级的 `index`。后续指令用这个 `index` 取值时，就会落到 STATIC 这一级。

### 4.1.4 代码实践

**实践目标**：直观感受「LOCAL/CLOSURE 换指针、GLOBAL/STATIC 不换」。

**操作步骤**：

1. 在 [src/njs_function.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_function.c) 第 575 行（`vm->levels[NJS_LEVEL_LOCAL] = vm->top_frame->local;`）之后，临时加一行打印（仅用于学习，**不要提交**）：
   ```c
   fprintf(stderr, "ENTER f: LOCAL=%p GLOBAL=%p STATIC=%p\n",
           (void*)vm->levels[NJS_LEVEL_LOCAL],
           (void*)vm->levels[NJS_LEVEL_GLOBAL],
           (void*)vm->levels[NJS_LEVEL_STATIC]);
   ```
2. 重新 `make njs`，运行 `./build/njs -c 'function f(){return 1} f()'`。
3. 对照每次进入函数时的三组地址。

**需要观察的现象**：LOCAL 的地址在「进入 f」前后会变化；GLOBAL 和 STATIC 的地址应当**始终不变**。

**预期结果**：随着 `f()` 被调用，stderr 会打印出 ENTER 行，其中 LOCAL 指针不同于全局代码阶段的 LOCAL 指针，而 GLOBAL/STATIC 保持同一个值。这正好印证「调用边界只换 LOCAL/CLOSURE」。

> 说明：若不便改源码，这一步可作为「源码阅读型实践」——直接对照 [src/njs_function.c:L568-L601](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_function.c#L568-L601) 说出「保存了哪两级、替换了哪两级、恢复了哪两级」即可。

### 4.1.5 小练习与答案

**练习 1**：为什么函数调用时不把 LOCAL 数组里的值逐个压栈，而是整体换指针？

> **参考答案**：逐个压栈/出栈要为每个值做一次拷贝，开销大；整体换指针是 O(1) 操作（只改两个指针），且原函数的局部数组完整保留在堆上，返回后无需恢复任何单个值。这正是寄存器式 VM 相对栈式 VM 的优势之一。

**练习 2**：STATIC 级的值为什么能跨 VM 克隆共享？

> **参考答案**：STATIC 存放的是字面量常量等「永久值」，它们在 `vm->shared`（跨克隆共享的只读资源，见 u2-l1、u2-l4）层面登记；克隆 VM 时只重建私有 runtime（LOCAL/CLOSURE 等可变部分），STATIC 指针指向的常量池保持共享，所以不会因克隆而复制。

---

## 4.2 index 位编码：用一个 32 位整数携带「槽位号 + 层级 + 变量类型」

### 4.2.1 概念说明

字节码指令的操作数（绝大多数）就是一个 `njs_index_t`。njs 没有为「取值」单独编码层级和类型两个操作数，而是把它们**打包进同一个 32 位整数**。这样一条 `MOVE`/`ADD` 指令的操作数既说明了「去哪一级存储」，也说明了「去第几号槽」，还顺带捎上了「这个槽的变量类型」——后者主要用于 `let`/`const` 的 TDZ 判断。

位布局如下（低位在右）：

\[ \text{index} = \underbrace{\text{slot}}_{\text{bits }31\ldots8\text{（24 位）}} \;\ll\; 8 \;\Big|\; \underbrace{\text{level}}_{\text{bits }7\ldots4\text{（4 位）}} \;\ll\; 4 \;\Big|\; \underbrace{\text{var\_type}}_{\text{bits }3\ldots0\text{（4 位）}} \]

对应三个宽度的宏在 [src/njs_scope.h:L10-L26](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_scope.h#L10-L26)：

```c
#define NJS_SCOPE_VAR_OFFSET    0          // 变量类型：低 4 位
#define NJS_SCOPE_VAR_SIZE      4
#define NJS_SCOPE_TYPE_OFFSET   (0 + 4)    // 存储层级：中 4 位
#define NJS_SCOPE_TYPE_SIZE     4
#define NJS_SCOPE_VALUE_OFFSET  (4 + 4)    // 槽位号：高 24 位
#define NJS_SCOPE_VALUE_SIZE    24
#define NJS_SCOPE_VALUE_MASK    ((1 << 24) - 1)   // 0x00FFFFFF
#define NJS_SCOPE_VAR_MASK      ((1 << 4) - 1)    // 0x0000000F
#define NJS_SCOPE_TYPE_MASK     ((1 << 4) - 1)    // 0x0000000F
#define NJS_SCOPE_VALUE_MAX     NJS_SCOPE_VALUE_MASK   // 单级最多 ~1600 万槽
```

变量类型占低 4 位，取值见 [src/njs_variable.h:L11-L17](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_variable.h#L11-L17)：`CONST=0, LET=1, CATCH=2, VAR=3, FUNCTION=4`。

### 4.2.2 核心流程

**组装 index** 的入口是 `njs_scope_index(scope, slot, level, var_type)`，它把四段拼成一个整数。这里有一个对理解反汇编**至关重要**的规则：**编译期一律按 `NJS_LEVEL_LOCAL` 申请槽位；但如果变量处在全局作用域（`NJS_SCOPE_GLOBAL`），就把层级改写成 `NJS_LEVEL_GLOBAL`**。也就是说，全局变量最终落在 GLOBAL 级，而不是 LOCAL 级。

伪代码：

```
function njs_scope_index(scope, slot, level, var_type):
    if slot > 0xFFFFFF:  return ERROR       // 槽位号溢出
    if scope == GLOBAL and level == LOCAL:
        level = GLOBAL                       # 全局作用域改写规则
    return (slot << 8) | (level << 4) | var_type
```

**解码 index** 则是三个独立的内联函数，分别取出三段：

```
var_type  = index & 0xF                       # njs_scope_index_var
level     = (index >> 4) & 0xF                # njs_scope_index_type
slot      = index >> 8                        # njs_scope_index_value
```

> 编译期作用域 `NJS_SCOPE_*` 与运行期层级 `NJS_LEVEL_*` 的映射就在「改写规则」这一步完成：`NJS_SCOPE_GLOBAL` 的变量 → `NJS_LEVEL_GLOBAL`；`NJS_SCOPE_FUNCTION` 的变量 → `NJS_LEVEL_LOCAL`（保留）；`NJS_SCOPE_BLOCK` 的 `let`/`const` → 归属到最近函数作用域，同样落 `NJS_LEVEL_LOCAL`（或被闭包捕获则落 CLOSURE）。

### 4.2.3 源码精读

**组装函数与改写规则** —— [src/njs_scope.h:L36-L53](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_scope.h#L36-L53)：

```c
njs_inline njs_index_t
njs_scope_index(njs_scope_t scope, njs_index_t index, njs_level_type_t type,
                njs_variable_type_t var_type)
{
    njs_assert(type < NJS_LEVEL_MAX);
    njs_assert(scope == NJS_SCOPE_GLOBAL || scope == NJS_SCOPE_FUNCTION);

    if (index > NJS_SCOPE_VALUE_MAX) {
        return NJS_INDEX_ERROR;
    }

    if (scope == NJS_SCOPE_GLOBAL && type == NJS_LEVEL_LOCAL) {
        type = NJS_LEVEL_GLOBAL;          // ★ 全局作用域改写规则
    }

    return (index << NJS_SCOPE_VALUE_OFFSET) | (type << NJS_SCOPE_TYPE_OFFSET)
            | var_type;
}
```

**变量在编译期拿到 index 的分配点** —— [src/njs_variable.c:L80-L81](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_variable.c#L80-L81)（函数声明路径，普通变量路径在 [L282](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_variable.c#L282) 同理）：

```c
var->index = njs_scope_index(root->type, root->items, NJS_LEVEL_LOCAL,
                             NJS_VARIABLE_FUNCTION);
```

注意第 3 个实参恒为 `NJS_LEVEL_LOCAL`——交给 `njs_scope_index` 后，若 `root->type` 是 `NJS_SCOPE_GLOBAL`，它会被改写成 `NJS_LEVEL_GLOBAL`。这就是为什么一个全局 `var a` 在字节码里是 GLOBAL 级而不是 LOCAL 级。

**三个解码函数** —— [src/njs_scope.h:L56-L75](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_scope.h#L56-L75)：

```c
njs_inline njs_variable_type_t
njs_scope_index_var(njs_index_t index) {
    return (njs_variable_type_t) (index & NJS_SCOPE_VAR_MASK);          // 低 4 位
}

njs_inline njs_level_type_t
njs_scope_index_type(njs_index_t index) {
    return (njs_level_type_t) ((index >> NJS_SCOPE_TYPE_OFFSET)
                               & NJS_SCOPE_TYPE_MASK);                   // 中 4 位
}

njs_inline uint32_t
njs_scope_index_value(njs_index_t index) {
    return (uint32_t) (index >> NJS_SCOPE_VALUE_OFFSET);                 // 高 24 位
}
```

### 4.2.4 代码实践

**实践目标**：手工把反汇编里的 hex 操作数拆成三段，并判断层级。

**操作步骤**：

1. 阅读 [src/njs_scope.h:L36-L53](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_scope.h#L36-L53) 的 `njs_scope_index`，确认位布局。
2. 取一个操作数 `0123`（十六进制），按解码三式手算：
   - `var_type = 0x0123 & 0xF = 0x3` → 查 [njs_variable.h:L11-L17](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_variable.h#L11-L17) 得 `NJS_VARIABLE_VAR`
   - `level = (0x0123 >> 4) & 0xF = 0x2` → 查 [njs_vm.h:L109-L115](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.h#L109-L115) 得 `NJS_LEVEL_GLOBAL`
   - `slot = 0x0123 >> 8 = 0x1` → 槽位 1
3. 把 `0x0123` 写成二进制核对：`0000 0001 0010 0011`，从右往左四四分组正是 `var=3 | level=2 | slot=1`。

**预期结果**：`0123` 表示「GLOBAL 级、第 1 号槽、VAR 类型」。完整拆解见第 5 节综合实践。

> 注意：`0123` 的 level 是 2（GLOBAL）而非 0（LOCAL），正是因为编译期它以 LOCAL 申请、却在全局作用域被 `njs_scope_index` 改写成 GLOBAL。这是初学者最易看漏的一步。

### 4.2.5 小练习与答案

**练习 1**：把 `0233` 拆成三段，并说明它指向哪一级、第几槽、什么变量类型。

> **参考答案**：`var_type = 0x0233 & 0xF = 3`（VAR）；`level = (0x0233 >> 4) & 0xF = 3`（STATIC）；`slot = 0x0233 >> 8 = 2`。所以它是 STATIC 级第 2 号槽、VAR 类型——通常是一个字面量常量。

**练习 2**：为什么 `var_type` 只给 4 位？这够用吗？

> **参考答案**：njs 的变量类型只有 CONST/LET/CATCH/VAR/FUNCTION 五种，4 位可表示 0–15，绰绰有余。更重要的是 `var_type` 在这里被复用做 TDZ 判断：因为 `CONST=0`、`LET=1` 排在最前，运行期只需判断 `var_type <= NJS_VARIABLE_LET` 就知道这个槽需要「未初始化即报错」的保护（见 4.3.3）。

---

## 4.3 levels 寻址：从 index 到 `njs_scope_value`

### 4.3.1 概念说明

有了四级存储和打包好的 `index`，运行期取值就是一次「解码 + 二维下标」：先用 `njs_scope_index_type/value` 从 `index` 里取出 level 和 slot，再去 `vm->levels[level][slot]` 取出那个 `njs_value_t *`。整个过程只有两次移位和一次指针解引用，是真正的 O(1) 寻址，没有任何哈希查找或字符串比较。

负责这件事的核心函数是 `njs_scope_value(vm, index)`。在此基础上还有两个常用变体：

- `njs_scope_valid_value(vm, index)`：取值**并兼任 TDZ 守卫**——如果槽位还没被初始化（`njs_is_valid` 为假）且变量是 `let`/`const`，就抛 `ReferenceError`；否则把它当 `undefined` 返回。
- `njs_scope_value_set(vm, index, value)`：把一个值指针写进指定槽位。

### 4.3.2 核心流程

取值流程（以一条 `ADD dst, src1, src2` 为例）：

```
对于每个操作数 index（如 0103）：
    level = njs_scope_index_type(index)    # → LOCAL
    slot  = njs_scope_index_value(index)   # → 1
    ptr   = vm->levels[level][slot]        # → 一个 njs_value_t*
    # ptr 指向真正的值（数字、字符串、对象……）
```

用公式写更直观：

\[ \text{value\_ptr} = \texttt{vm->levels}\big[\,\texttt{type}(\text{index})\,\big]\big[\,\texttt{slot}(\text{index})\,\big] \]

TDZ 守卫（`njs_scope_valid_value`）的判断逻辑：

```
value = njs_scope_value(vm, index)
if 值未初始化 (njs_is_valid == false):
    if var_type <= NJS_VARIABLE_LET:       # CONST(0) 或 LET(1)
        抛 ReferenceError("cannot access variable before initialization")
        return NULL
    else:                                   # VAR / FUNCTION / CATCH
        njs_set_undefined(value)            # 当成 undefined
return value
```

这就是 JS「在 `let`/`const` 声明之前访问会报错，而 `var` 只是 `undefined`」在引擎层的实现。

### 4.3.3 源码精读

**核心寻址函数** —— [src/njs_scope.h:L78-L83](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_scope.h#L78-L83)：

```c
njs_inline njs_value_t *
njs_scope_value(njs_vm_t *vm, njs_index_t index)
{
    return vm->levels[njs_scope_index_type(index)]
                     [njs_scope_index_value(index)];
}
```

两行就完成了「解码 + 二维下标」，没有任何循环或查找——这正是寄存器式 VM 取值快的根源。

**TDZ 守卫** —— [src/njs_scope.h:L86-L104](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_scope.h#L86-L104)：

```c
njs_inline njs_value_t *
njs_scope_valid_value(njs_vm_t *vm, njs_index_t index)
{
    njs_value_t  *value;

    value = njs_scope_value(vm, index);

    if (!njs_is_valid(value)) {
        if (njs_scope_index_var(index) <= NJS_VARIABLE_LET) {   # let/const
            njs_reference_error(vm, "cannot access variable "
                                    "before initialization");
            return NULL;
        }
        njs_set_undefined(value);                                # var → undefined
    }

    return value;
}
```

注意它复用了 4.2 讲过的 `njs_scope_index_var(index)`：从同一个 `index` 里抠出变量类型，决定该不该报错。`index` 一物两用（既定位槽位、又携带变量类型）在这里体现得淋漓尽致。

**写槽位** —— [src/njs_scope.h:L107-L112](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_scope.h#L107-L112)，和取值对称：

```c
njs_inline void
njs_scope_value_set(njs_vm_t *vm, njs_index_t index, njs_value_t *value)
{
    vm->levels[njs_scope_index_type(index)]
              [njs_scope_index_value(index)] = value;
}
```

**槽位数组是怎么分配出来的** —— [src/njs_scope.c:L28-L54](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_scope.c#L28-L54)（`njs_scope_make`）：一次性从内存池分配「`count` 个指针 + `count` 个值」的连续内存，让第 `i` 个指针指向第 `i` 个值，并把每个值初始化为 `invalid`（即未初始化态，正是 TDZ 判断的依据）：

```c
size = (count * sizeof(njs_value_t *)) + (count * sizeof(njs_value_t));
refs = njs_mp_alloc(vm->mem_pool, size);
...
values = (njs_value_t *) ((u_char *) refs + (count * sizeof(njs_value_t *)));
while (count != 0) {
    count--;
    refs[count] = &values[count];
    njs_set_invalid(refs[count]);     # 标记为未初始化
}
return refs;
```

**临时槽位分配** —— [src/njs_scope.c:L15-L25](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_scope.c#L15-L25)（`njs_scope_temp_index`）：表达式求值的中间结果（如 `a + b` 的和）也需要槽位，它就近在当前函数作用域里取一个递增的 LOCAL 槽：

```c
njs_index_t
njs_scope_temp_index(njs_parser_scope_t *scope)
{
    scope = njs_function_scope(scope);
    if (njs_slow_path(scope == NULL)) {
        return NJS_INDEX_ERROR;
    }
    return njs_scope_index(scope->type, scope->items++, NJS_LEVEL_LOCAL,
                           NJS_VARIABLE_VAR);
}
```

这些临时槽位会落 LOCAL 级，函数返回时随 LOCAL 数组整体作废，无需单独回收。

### 4.3.4 代码实践

**实践目标**：用 `-d` 反汇编，把每条指令的操作数都映射到具体的存储层级，验证「解码 → 二维下标」模型。

**操作步骤**：

1. 构建 CLI（u1-l3）：`./configure && make njs`。
2. 运行（参考 [docs/agent/engine-dev.md](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/docs/agent/engine-dev.md) 的字节码示例）：
   ```bash
   ./build/njs -d
   >> var a = 42; function f(v) { return v + 1 }
   ```
   预期输出：
   ```
   shell:main
       1 | 00000 MOVE     0123 0133
       1 | 00024 STOP     0033
   shell:f
       1 | 00000 ADD      0203 0103 0233
       1 | 00032 RETURN   0203
   ```
3. 逐个操作数套用 4.2.4 的解码三式。
4. 在源码里确认取值路径：解释器执行 `MOVE` 时，对 `dst`、`src` 两个操作数各调用一次 [src/njs_scope.h:L78-L83](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_scope.h#L78-L83) 的 `njs_scope_value`，得到两个 `njs_value_t *`，再把 src 的内容拷给 dst。

**需要观察的现象**：所有操作数都能被唯一地拆成「层级 + 槽位 + 变量类型」，且与该指令的语义自洽（`MOVE` 的 src 是常量、dst 是全局变量；`ADD` 的两个源分别是函数参数和常量）。

**预期结果**：见下一节综合实践的完整拆解表。

### 4.3.5 小练习与答案

**练习 1**：`njs_scope_value` 为何不检查 `index` 合法性（越界等）？

> **参考答案**：`index` 是编译期由 `njs_scope_index` 生成的，槽位号在生成时已受 `NJS_SCOPE_VALUE_MAX` 上限校验（见 [src/njs_scope.h:L43-L45](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_scope.h#L43-L45)），层级受 `njs_assert(type < NJS_LEVEL_MAX)` 保护。运行期这些约束恒成立，再做检查纯属浪费——这是寄存器式 VM「编译期保证、运行期直奔内存」的典型取舍。

**练习 2**：把 `let x = 1; console.log(x)` 放在声明前访问（`console.log(x); let x = 1;`），引擎会在哪段代码里报错？

> **参考答案**：在取值时由 [src/njs_scope.h:L86-L104](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_scope.h#L86-L104) 的 `njs_scope_valid_value` 报错：因为 `x` 的槽位尚未初始化（`njs_is_valid` 为假）且 `var_type` 是 `LET(1) <= NJS_VARIABLE_LET`，于是抛 `ReferenceError("cannot access variable before initialization")`。若把 `let` 换成 `var`，同一处不会报错，而是把空槽当 `undefined` 返回。

---

## 5. 综合实践：完整解码 `var a = 42; function f(v) { return v + 1 }` 的字节码

本任务把「四级存储 + index 编码 + levels 寻址」三块串起来。请对 `./build/njs -d` 给出的下面这段字节码，逐操作数完成解码并回答「它在哪一级、第几槽」。

```
shell:main
    1 | 00000 MOVE     0123 0133
    1 | 00024 STOP     0033
shell:f
    1 | 00000 ADD      0203 0103 0233
    1 | 00032 RETURN   0203
```

**步骤 1：先弄清操作数顺序。** 反汇编对 `MOVE`/`ADD` 这类指令按「目的在前、源在后」打印（见 [src/njs_disassembler.c:L546-L558](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_disassembler.c#L546-L558)，先打 `dst` 再打 `src`），与指令结构 [src/njs_vmcode.h:L133-L152](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.h#L133-L152) 的字段顺序一致。所以 `MOVE 0123 0133` 是「把 `0133` 的值拷到 `0123`」，`ADD 0203 0103 0233` 是「`0203 = 0103 + 0233`」（与 [docs/agent/engine-dev.md:L245-L247](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/docs/agent/engine-dev.md#L245-L247) 的说明一致）。

**步骤 2：逐操作数解码。** 套用 `var = &0xF`、`level = (>>4)&0xF`、`slot = >>8`：

| 操作数 | 二进制（低 16 位） | var_type | level | slot | 语义解读 |
|---|---|---|---|---|---|
| `0123`（MOVE 的 dst） | `0001 0010 0011` | `3` VAR | `2` GLOBAL | `1` | 全局变量 `a`（全局作用域第 1 号槽，`this` 占 0 号） |
| `0133`（MOVE 的 src） | `0001 0011 0011` | `3` VAR | `3` STATIC | `1` | 字面量常量 `42` |
| `0033`（STOP） | `0000 0011 0011` | `3` VAR | `3` STATIC | `0` | 全局返回值槽（STATIC 0 号） |
| `0203`（ADD 的 dst / RETURN） | `0010 0000 0011` | `3` VAR | `0` LOCAL | `2` | 函数 `f` 的临时结果槽（LOCAL 2 号） |
| `0103`（ADD 的 src1） | `0001 0000 0011` | `3` VAR | `0` LOCAL | `1` | 函数 `f` 的参数 `v`（LOCAL 1 号） |
| `0233`（ADD 的 src2） | `0010 0011 0011` | `3` VAR | `3` STATIC | `2` | 字面量常量 `1` |

**步骤 3：用人话读出整段程序。**

- `MOVE 0123 0133`：把 STATIC 第 1 号槽里的常量 `42` 拷进 GLOBAL 第 1 号槽——正是 `var a = 42`。注意 `a` 是 GLOBAL 级而非 LOCAL 级，这正是 4.2.3 的「全局作用域改写规则」在字节码里的体现。
- `ADD 0203 0103 0233`：在函数 `f` 内部，`LOCAL 1`（参数 `v`）加上 `STATIC 2`（常量 `1`），结果放进 `LOCAL 2`（临时槽）。这正是 `return v + 1` 的求值。
- `RETURN 0203`：返回 `LOCAL 2`（刚算出的 `v + 1`）。函数体里所有操作数都在 LOCAL/STATIC，没有任何 GLOBAL——因为函数体不直接碰全局变量。
- `STOP 0033`：全局代码结束，返回 STATIC 0 号槽（全局 retval）。

**步骤 4：连接到运行期寻址。** 对 `MOVE 0123 0133`，解释器对两个操作数各调一次 [src/njs_scope.h:L78-L83](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_scope.h#L78-L83) 的 `njs_scope_value`：

- `njs_scope_value(vm, 0x0123)` → `vm->levels[NJS_LEVEL_GLOBAL][1]` → 指向全局变量 `a` 的值；
- `njs_scope_value(vm, 0x0133)` → `vm->levels[NJS_LEVEL_STATIC][1]` → 指向常量 `42`。

两次移位 + 两次下标，值就拿到了。

**预期结果**：你能不查源码，仅凭一个 hex 操作数就说出它属于 LOCAL/CLOSURE/GLOBAL/STATIC 哪一级、第几号槽；并能解释为什么全局变量 `a` 的 level 是 2 而不是 0。

> 待本地验证：上表的「语义解读」列（`a`/`v`/`42`/`1` 等）是基于源码逻辑与 [docs/agent/engine-dev.md](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/docs/agent/engine-dev.md) 示例的推断；不同 njs 版本生成的槽位号可能微调，请以你本地 `./build/njs -d` 的实际输出为准核对——但解码三式（var/level/slot）恒成立。

---

## 6. 本讲小结

- njs 把所有运行期值槽分成 **LOCAL / CLOSURE / GLOBAL / STATIC 四级**，VM 用 `vm->levels[NJS_LEVEL_MAX]` 这组二级指针统一管理；函数调用时**只换 LOCAL 和 CLOSURE 两个指针**，GLOBAL/STATIC 全程不动。
- 每个值槽的「地址」被打包进一个 32 位 `njs_index_t`：**低 4 位变量类型、中 4 位存储层级、高 24 位槽位号**。组装函数 [src/njs_scope.h:L36-L53](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_scope.h#L36-L53) 有一条关键改写规则——**全局作用域的变量层级从 LOCAL 改写为 GLOBAL**。
- 运行期取值 [src/njs_scope.h:L78-L83](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_scope.h#L78-L83) 只做「解码 level/slot + 二维下标 `vm->levels[level][slot]`」，是 O(1) 无查找访问，这正是寄存器式 VM 取值快的根因。
- `njs_scope_valid_value` 复用同一个 `index` 里的 `var_type` 实现 **TDZ 守卫**：`let`/`const` 槽未初始化就报 `ReferenceError`，`var` 则当 `undefined`。
- 编译期作用域（`NJS_SCOPE_GLOBAL/FUNCTION/BLOCK`）与运行期层级（`NJS_LEVEL_LOCAL/CLOSURE/GLOBAL/STATIC`）是两个维度，二者的映射发生在 `njs_scope_index` 的改写规则这一步。
- 反汇编里 `MOVE 0123 0133` 这类 hex 操作数，都可以用「`&0xF` 取变量类型、`(>>4)&0xF` 取层级、`>>8` 取槽位号」三式完整解码。

## 7. 下一步学习建议

本讲搞清楚了「值存在哪、怎么取」，接下来的学习方向：

- **u4-l3 函数调用与调用帧**：深入 [src/njs_function.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_function.c) 的 `njs_function_frame_invoke`，弄清 `vm->top_frame->local` 这个 LOCAL 数组是怎么随调用栈一层层建立和回收的，以及 CLOSURE 数组（`njs_function_closures`）如何承载被捕获的变量。
- **u4-l4 异常处理**：看 `try/catch/throw` 在字节码层如何用跳转实现，并注意异常发生时沿调用帧回溯的过程同样依赖 LOCAL/CLOSURE 指针的正确恢复。
- **回看 u3-l3**：现在再读 `njs_variable.c` 里变量分配 `index` 的代码（[L80](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_variable.c#L80)、[L282](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_variable.c#L282)），你会对「编译期分配 → 运行期寻址」这条链有闭环的理解。
