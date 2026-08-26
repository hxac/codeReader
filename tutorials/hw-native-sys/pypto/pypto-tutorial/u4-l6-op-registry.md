# 算子注册表 OpRegistry

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出算子注册表的组织方式：一个进程级单例，以「带命名空间的名字」为键，每个条目（`OpRegistryEntry`）携带一份完整的算子元信息。
2. 拆解一条注册记录里的五类信息：必备元数据（描述/类别/参数表/类型推断函数）、内存空间规格、参数副作用、跨核角色、internal-only 标记。
3. 跟踪 `Create` → `CreateImpl` 的建 Call 流程：查表 → internal-only 门禁 → kwargs 校验 → 类型推断 → 内存空间回填。
4. 解释为什么算子身份是**名字**而不是指针，并会用 `IsOp` 写出「拼错即报错」的比较。
5. 讲清 `init_cond` 这类可依赖循环值的可选参数，为什么注册成**操作数**而不是 kwarg，以及它在四个累加算子上的注册差异。

本讲承接 u4-l5（Function 与 Program）：`Call` 节点的 `op_` 字段指向的那个「算子对象」从哪里来、长什么样，就是本讲要解剖的内容。

## 2. 前置知识

### 2.1 算子（Op）与函数（GlobalVar）是两种被调者

IR 里一个 `Call` 表达式的 `op_` 可能指向两种东西：

| 被调者 | 类型 | 语义 | 例子 |
| --- | --- | --- | --- |
| 注册算子 | `Op` | 内建操作，名字全局唯一、语义由编译器各层共同实现 | `tile.load`、`tensor.add` |
| 全局函数 | `GlobalVar`（继承自 `Op`） | 用户写的函数，名字是函数名 | `@pl.function def main(...)` |

注册表只管第一类。第二类的名字**不**在注册表里——这正是 `CreateImpl` 在查表失败时给出额外提示的原因（见 4.3 节）。

### 2.2 操作数（operand）与 kwarg 的区别

这是本讲最重要的一组概念，直接决定 4.5 节的结论：

| 维度 | 操作数（`args_`） | kwarg（`kwargs_`） |
| --- | --- | --- |
| 存放内容 | SSA 值（`ExprPtr`） | 编译期常量（bool/int/string/double/DataType/MemorySpace 等） |
| 参与数据流 | 是，进 use-def 链，可被 Pass 重写、替换、DCE | 否，是元数据，只在建 Call 与发射时被读取 |
| 可否依赖循环变量 | **可以**（如 `k0 == 0` 这个比较表达式） | **不可以**，只能是常量 |
| 类型检查 | 由 `f_deduce_type` 逐算子校验 | 由 `ValidateKwargs` 按 schema 校验 |

一句话：**值会变的放操作数，编译期就定死的放 kwarg。**

### 2.3 需要的前置认知

- u3-l1 讲过解析器把 `pl.tile.xxx(...)` 翻译成 `Call`——本讲讲那个 `Call` 里的 `op_` 是怎么造出来的。
- u2-l3/u2-l4 讲过 `pl.*` 调度器按实参类型分发给 `tensor.*` / `tile.*`——本讲讲这两百多个名字在 C++ 侧的家。
- u4-l4 讲过类型推断分「通用层 + 算子专属 deducer 层」——本讲讲的 `f_deduce_type` 就是把 deducer 挂到注册条目上的那个钩子。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [include/pypto/ir/op_registry.h](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/op_registry.h) | 注册表全部声明：`OpRegistryEntry` 流式注册 API、`OpRegistry` 单例、`IsOp` 辅助、`REGISTER_OP` 宏 |
| [src/ir/op_registry.cpp](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/op_registry.cpp) | 实现：`Register` / `CreateImpl` / `ValidateKwargs` / `ValidateTileOps` / `ValidateArgEffects` |
| [src/ir/op/](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/op) 目录（47 个文件用 `REGISTER_OP`） | 每个算子的**实际注册点**：`tile_ops/matmul.cpp`、`tile_ops/memory.cpp`、`tensor_ops/`、`distributed/` 等 |
| [python/bindings/modules/ir.cpp](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/bindings/modules/ir.cpp) | Python 面：`create_op_call` / `get_op` / `get_op_memory_spec` / `get_op_arg_effect` 等绑定 |
| [python/pypto/ir/op/tile_ops.py](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/ir/op/tile_ops.py) | Python IR 封装层：`tile.matmul_acc(...)` 这类函数最终落到 `create_op_call` |
| [python/pypto/ir/operators.py](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/ir/operators.py) | 给 `Expr` 打运算符重载补丁（`+` → `add`），也走 `create_op_call` |
| [docs/en/dev/ir/05-operators.md](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/ir/05-operators.md) | 算子权威文档，含 `init_cond` 专节 |

注意一个容易混淆的点：注册表的**声明与机制**在 `src/ir/op_registry.cpp`（439 行），而**具体算子的注册语句**散布在 `src/ir/op/` 下的 47 个文件里。本讲两个都读。

## 4. 核心概念与源码讲解

### 4.1 注册表骨架：单例、命名空间与 REGISTER_OP

#### 4.1.1 概念说明

`OpRegistry` 是一个进程级单例，本质是一张 `unordered_map<string, OpRegistryEntry>`：键是算子全名（如 `"tile.matmul_acc"`），值是那个算子的全部元信息。

算子名采用**点分命名空间**约定，名字的第一段就是命名空间。统计当前 HEAD 下 47 个注册文件里的 `REGISTER_OP("...")` 调用，各命名空间的算子数量为：

| 命名空间 | 数量 | 含义 |
| --- | --- | --- |
| `tile.` | 138 | Tile 级（片上）算子，一一映射硬件指令 |
| `tensor.` | 106 | Tensor 级（全局内存）算子，算法视角 |
| `pld.` | 23 | 分布式 rank 视角算子（`pld.system.notify` 等） |
| `system.` | 22 | 系统级指令（fence、cacheinvalid、sync） |
| `builtin.` | 8 | Pass 降级产出的**编译器内部**算子（`builtin.tensor.allreduce` 等） |
| `dist.` / `prefetch.` / `test.` / `array.` | 5 / 4 / 4 / 3 | 其余内部分类 |

这个分布印证了 u2 的经验：Tile 级算子最多（性能层最丰富），Tensor 级次之（算法层收敛）。

#### 4.1.2 核心流程

注册发生在**静态初始化期**，早于任何 Python 代码执行：

```text
进程启动
  └─ C++ 静态初始化
       └─ 47 个 op/*.cpp 里的 REGISTER_OP 宏各执行一次
            └─ OpRegistry::Register("tile.matmul_acc")
                 ├─ 查重（重复注册 → ValueError）
                 ├─ 在 map 里创建空 entry 并 set_name
                 └─ 创建共享的 Op 实例（携带 kwarg schema）
       └─ 流式调用链把元信息逐项填进 entry
  └─ Python import pypto（bindings.cpp）
       ├─ ValidateTileOps()   ← 每个 tile.* 必须有内存规格
       └─ ValidateArgEffects() ← 每个原地算子必须声明参数副作用
```

两道 import 期校验是「快速失败」的关键：一个新算子忘了声明内存规格，用户在 `import pypto` 时就会得到列出所有缺失项的报错，而不是在编译某个具体算子时才崩。

#### 4.1.3 源码精读

**注册宏**——利用静态变量的初始化副作用，把注册语句挂到静态初始化期：

[include/pypto/ir/op_registry.h:1176-1178](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/op_registry.h#L1176-L1178) 定义 `REGISTER_OP` 宏：声明一个 `__COUNTER__` 编号的静态 `OpRegistryEntry&` 引用变量，并用 `OpRegistry::GetInstance().Register(OpName)` 的返回值初始化它。静态变量必然被初始化，注册因此必然发生；`PYPTO_UNUSED` 抑制「变量未使用」告警。

**Register 实现**——查重、建条目、造 Op 三步：

[src/ir/op_registry.cpp:187-200](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/op_registry.cpp#L187-L200) 先用 `CHECK` 断言该名字不在表里（重复注册是编程错误，走用户错误通道），然后 `registry_.emplace` 创建空条目、`set_name` 写入名字，最后 `std::make_shared<Op>(op_name)` 造出那个会被千万个 `Call` 共享的 `Op` 实例。

**单例本身**：

[src/ir/op_registry.cpp:182-185](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/op_registry.cpp#L182-L185) 是标准的 Meyers 单例（函数内 `static` 局部变量）。头文件注释明确说明：**注册期非线程安全**，要求所有注册在并发访问开始前（即静态初始化期）完成。

**import 期两道校验**：

[python/bindings/bindings.cpp:72-77](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/bindings/bindings.cpp#L72-L77) 在所有绑定注册完成后立即调用 `ValidateTileOps()` 与 `ValidateArgEffects()`。注释直白地写出动机：「fails at import time if any are missing」与「an undeclared writer reads as a pure consumer and its dependency edge vanishes」。

#### 4.1.4 代码实践

**实践目标**：用源码统计验证本讲的命名空间数量表，并亲手触发一次「查无此算子」的报错路径。

**操作步骤**：

1. 统计各命名空间的注册数（在仓库根目录执行）：

```bash
grep -rhoE 'REGISTER_OP\("[a-z]+[.][^"]*"' src/ \
  | sed -E 's/REGISTER_OP\("([a-z]+)\..*/\1/' \
  | sort | uniq -c | sort -rn
```

2. 数出 `tile.` 命名空间的具体清单（应与上一步的 tile 数量一致）：

```bash
grep -rhoE 'REGISTER_OP\("tile\.[^"]*"' src/ | sed -E 's/REGISTER_OP\("(.*)"/\1/' | sort
```

3. 在 Python 里查询一个不存在的算子，观察报错：

```python
from pypto.pypto_core import ir as _ir_core

_ir_core.get_op("tile.reshaep")   # 故意拼错 reshape
```

**需要观察的现象**：

- 第 1 步输出与 4.1.1 的表格一致（tile 138 / tensor 106 / …）。若你本地的数字更大，说明 HEAD 之后又新增了算子——记下差异即可。
- 第 3 步抛出 `ValueError`，消息形如 `Operator 'tile.reshaep' not found in registry`。

**预期结果**：注册表规模约 300+ 个算子；名字是唯一入口，拼错立刻报错而不是静默返回 None。第 3 步的具体报错文案**待本地验证**（不同版本消息措辞可能略有差异）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `REGISTER_OP` 用「静态变量初始化」而不是让用户在某处显式调用一个 `register_all_ops()`？

**答案**：静态初始化由链接器保证在 `main`/模块加载时全部执行，新增算子只需在某个 `.cpp` 里写一行 `REGISTER_OP`，无需修改任何中心化清单；同时也让 `bindings.cpp` 里的两道 import 期校验必然看到完整的表。代价是注册期不能并发——头文件注释明确要求所有注册在并发访问前完成。

**练习 2**：`src/ir/op/README.md` 里的目录组织（`tensor_ops/`、`tile_ops/` 按类别分文件）与注册表的命名空间是什么关系？

**答案**：二者**约定一致但无强制耦合**。注册表的键是字符串，命名空间由名字的第一段决定；目录与文件只是物理组织，便于人找代码。理论上一个 `tile.*` 算子写在 `tensor_ops/` 下也能注册成功——但 `ValidateTileOps` 只按名字前缀 `tile.` 检查内存规格，所以放错目录不会被拦截，靠的是评审纪律。

---

### 4.2 OpRegistryEntry：一条注册记录携带的元信息

#### 4.2.1 概念说明

`OpRegistryEntry` 是「一个算子的档案卡」。它解决的问题是：IR 里两百多个算子，每个都有名字、描述、参数表、类型推断规则、内存约束、副作用……这些信息如果散落在各处，Pass 与代码生成就只能靠 `if (name == ...)` 硬编码。注册表把它们集中成一份**声明式规格**，供所有下游统一查询。

档案卡上的信息分五组：

| 组 | 关键字段 / 方法 | 用途 |
| --- | --- | --- |
| ① 必备元数据 | `description_`、`op_category_`、`arguments_`、`deduce_type_` | 文档、分类、参数表、结果类型推断 |
| ② 内存规格 | `memory_spec_`（`OpMemorySpaceSpec`） | 输入必须住在哪个内存空间、输出空间如何决定 |
| ③ 参数副作用 | `arg_effects_`（`OpArgEffectSpec`） | 该算子对每个参数是 Read / Write / ReadWrite |
| ④ 跨核与亲和 | `set_core_affinity`、`set_cross_core_role`、`set_no_duplicate` | 混合内核拆分、跨核流水的 Pass 决策 |
| ⑤ 可见性 | `set_internal_only` | 编译器内部算子，用户代码按名字调不到 |

其中 ① 是**强制的**：任何一项缺失，`GetOp()` 就会抛错。

#### 4.2.2 核心流程

一个算子从注册到可用的门槛：

```text
REGISTER_OP("tile.matmul_acc")     ← 进入注册表（条目存在但可能不完整）
  ├─ set_op_category / set_description / add_argument ×N   ← 必备四件套
  ├─ f_deduce_type(...)                                     ← 必备：类型推断函数
  ├─ set_input_memory / set_output_memory / set_output_reuses_input
  ├─ set_arg_effect(0, ArgEffect::ReadWrite)
  └─ ...
       ↓ 首次有人调用 GetOp() / Create()
  GetOp() 逐项 CHECK 四个必备字段 → 缺一项即 ValueError
       ↓ import pypto 时
  ValidateTileOps()    ← tile.* 必须有 memory_spec 或显式 no_memory_spec()
  ValidateArgEffects() ← set_output_reuses_input(N) 的算子必须对参数 N 声明副作用
```

#### 4.2.3 源码精读

**必备字段校验**——注册表自己当守门员：

[include/pypto/ir/op_registry.h:272-294](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/op_registry.h#L272-L294) 的 `GetOp()` 连续五个 `CHECK`：`op_` 实例、`description_`、`op_category_`、`arguments_`（必须显式 `add_argument` 或 `no_argument`）、`deduce_type_`。任何一项缺失都给出指名道姓的报错（如 `"...has no description. Use .set_description() to provide one."`）。

**② 内存规格**——`OpMemorySpaceSpec` 结构体：

[include/pypto/ir/op_registry.h:56-80](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/op_registry.h#L56-L80) 定义三件事：
- `input_constraints`：按参数下标列出「允许的内存空间集合」，空向量表示该参数不限制。例如 `tile.matmul` 要求参数 0 在 `Left`、参数 1 在 `Right`。
- `deduce_output_memory`：一个函数对象，从 call 的 kwargs 解析输出空间；返回 `nullopt` 表示「此处定不下来」，交给 pass 17（InferTileMemorySpace）按消费者需求决定。
- `output_reuses_input_arg`：**原地算子的标记**。注释点名 `matmul_acc`、`gemv_acc`——输出就是输入缓冲本身。

配套的流式设置方法分布在 [op_registry.h:469-477](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/op_registry.h#L469-L477)（`set_output_memory` 固定输出空间，如 matmul → `Acc`）、[op_registry.h:484-499](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/op_registry.h#L484-L499)（`set_output_memory_from_kwarg`，`tile.load` 用它读 `target_memory`，且缺省传 `nullopt` 即「可重定向」）、[op_registry.h:501-513](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/op_registry.h#L501-L513)（`set_output_memory_inherit_input`，视图类算子继承输入空间）、[op_registry.h:516-529](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/op_registry.h#L516-L529)（`set_input_memory`，单个或多个允许空间）、[op_registry.h:566-573](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/op_registry.h#L566-L573)（`set_output_reuses_input`）。

**③ 参数副作用**——`ArgEffect` 三值枚举：

[include/pypto/ir/op_registry.h:120-124](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/op_registry.h#L120-L124) 定义 `Read`（只读）、`Write`（只写、目标操作数）、`ReadWrite`（读且写——累加、原子更新、原地改写）。头文件的注释非常值得精读：`Write` 是**数据流声明**而非覆盖声明——它说「本调用不从这个缓冲读数据」，据此决定是否需要 host→device 暂存与 RAW 依赖边；它**不**承诺「目标每个字节都被重定义」，需要覆盖信息的分析必须另行确认写入区域。

[include/pypto/ir/op_registry.h:705-715](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/op_registry.h#L705-L715) 的 `set_arg_effect(arg_index, effect)` 是固定声明；[op_registry.h:721-730](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/op_registry.h#L721-L730) 是**带 resolver 的重载**——副作用由 kwarg 决定时用（例如 `tile.store` 的 `atomic` 参数决定它是覆写还是累加，见 4.2.4 实践）。

**import 期校验如何用这些信息**：

[src/ir/op_registry.cpp:371-388](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/op_registry.cpp#L371-L388) 的 `ValidateTileOps` 遍历所有 `tile.` 前缀条目，收集没有 `memory_spec` 的名字，一次性列全后抛错。[src/ir/op_registry.cpp:390-436](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/op_registry.cpp#L390-L436) 的 `ValidateArgEffects` 更进一步：对每个 `set_output_reuses_input(N)` 的算子，用 `HasDeclaredArgEffect(N)` 追问「注册到底有没有对**那个**参数表态」——只声明了别的参数不算数。注释解释了为什么这件事必须 import 期拦：方向推断把未声明的算子当纯消费者，写副作用被静默丢弃，依赖边凭空消失，问题最终会以设备上的竞态或死锁形式出现，而不是编译错误。

#### 4.2.4 代码实践

**实践目标**：用 Python 绑定查询 `tile.load` 与 `tile.store` 的档案卡，把「内存规格 + 参数副作用」两组信息读出来。

**操作步骤**：

```python
from pypto.pypto_core import ir as _ir_core

op = _ir_core.get_op("tile.load")
print("name:", op.name)                       # tile.load
print("kwarg schema:", op.get_attr_keys())    # ['clamp', 'target_memory']（顺序不定）
print("has target_memory:", op.has_attr("target_memory"))

# 内存规格：输入约束 + 输出空间如何决定
spec = _ir_core.get_op_memory_spec("tile.load")
print("input_constraints:", spec["input_constraints"])
print("output_memory:", spec["output_memory"])   # 期望 'deferred'

# 对照：输出空间固定的算子
print(_ir_core.get_op_memory_spec("tile.matmul")["output_memory"])   # 期望 MemorySpace.Acc

# 参数副作用：tile.store 的参数 2（输出张量）依 atomic 而定
print("plain :", _ir_core.get_op_arg_effect("tile.store", 2))
print("atomic:", _ir_core.get_op_arg_effect("tile.store", 2, atomic=1))
```

**需要观察的现象**：

- `tile.load` 的 `output_memory` 是字符串 `"deferred"`——因为注册时 `set_output_memory_from_kwarg("target_memory")` 没给默认值，输出空间交给 pass 17 决定（对照 [src/ir/op/tile_ops/memory.cpp:1100-1104](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/op/tile_ops/memory.cpp#L1100-L1104) 的注册注释「No fallback … InferTileMemorySpace picks the space from consumer demand」）。
- `tile.matmul` 的输出是具体的 `MemorySpace.Acc`。
- `tile.store` 参数 2 的副作用随 `atomic` 变化：不带 `atomic` 时为 `Write`（覆写），`atomic=1` 时为 `ReadWrite`（读出旧值再累加）。

**预期结果**：以上断言与 [src/ir/op/tile_ops/memory.cpp:1087-1108](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/op/tile_ops/memory.cpp#L1087-L1108)（`tile.load`）和 [src/ir/op/tile_ops/memory.cpp:1110-1137](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/op/tile_ops/memory.cpp#L1110-L1137)（`tile.store`）的注册语句一一对应。`tile.store` 的 resolver 读 `atomic` kwarg：等于 `AtomicType::kNone` 时返回 `Write`，否则 `ReadWrite`，还带 `set_write_channel(WriteChannel::Dma)`（MTE3 写路径）。具体打印文案**待本地验证**（enum 的字符串表示形式依绑定而定）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ValidateArgEffects` 要专门用 `HasDeclaredArgEffect(reused)` 追问「那个参数」，而不是看 `HasDeclaredArgEffects()`（整个算子有没有声明过）？

**答案**：`set_arg_effect` 会把 `per_arg` 向量扩到能容纳最高下标，因此「没人声明过的槽位」和「显式声明为 Read 的槽位」在 `per_arg` 里不可区分。一个算子可能声明了参数 0 的副作用却忘了声明它真正原地写的参数 2——只看「声明过没有」会漏掉这种情况。`declared_args` 集合（[op_registry.h:224](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/op_registry.h#L224) 附近）记录的正是「哪些下标被点名过」。

**练习 2**：`tile.load` 的 `output_memory` 为什么不能像 `tile.matmul` 那样直接注册成固定值？

**答案**：`tile.load` 把数据从 GM 搬进片上，目的地（Vec/Mat/Left/Right…）取决于**下游消费者**要什么。注册期给定值会锁死选择；注册成「deferred」让 pass 17（InferTileMemorySpace）看到完整的生产者-消费者上下文后再决定，必要时还能重写 `target_memory` kwarg（`HasRetargetableMemoryKwarg`，[op_registry.h:559-564](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/op_registry.h#L559-L564)）。

---

### 4.3 Call 的工厂：CreateImpl 的五步

#### 4.3.1 概念说明

注册表不只是被动数据库，它还是**建 `Call` 的唯一工厂**。无论来自 DSL 解析器、Python IR 封装层，还是某个 Pass 在重写 IR 时合成的新调用，最终都汇到 `OpRegistry::Create*`。这保证了三件好事：

1. kwargs 必过 schema 校验（未注册的 kwarg 名、类型不符都会被拦下）；
2. 结果类型必经 `f_deduce_type` 推断（不会出现无类型的 Call）；
3. 内存规格必被应用到推断出的 TileType 上（修掉 deducer 忘写 memory_space 的遗漏）。

#### 4.3.2 核心流程

```text
create_op_call("tile.load", args, kwargs, span)     ← Python 面
  └─ OpRegistry::CreateUserFacing(...)              ← internal_only 门禁：关
       └─ CreateImpl(name, args, kwargs, span, allow_internal=false)
            ① 查表：名字不在表里 → ValueError
               （名字不含 '.' 时附加「这像是函数名（GlobalVar）」的提示）
            ② internal_only 门禁：allow_internal=false 时拒绝内部算子
            ③ kwargs 校验：ValidateKwargs 逐个对照 Op 里登记的类型
            ④ 类型推断：调用 f_deduce_type(args, kwargs)
               ├─ 抛 PyPTO 异常 → 原样重抛，仅附加上 span 位置
               └─ 抛 std::exception → 折成 ValueError + span
            ⑤ 内存规格应用：
               ├─ CheckOperandMemorySpaceReachable：输入空间不可达即拒绝
               └─ deduce_output_memory / 继承输入 / deferred → 回填 TileType
            └─ make_shared<Call>(op, args, kwargs, result_type, span)
```

#### 4.3.3 源码精读

**三条创建通道**——可见性分级：

[src/ir/op_registry.cpp:206-236](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/op_registry.cpp#L206-L236) 定义三组入口：`Create`（编译器内部使用，`allow_internal=true`）、`CreateUserFacing`（用户可达路径，`allow_internal=false`）、`CreateInternal`（Pass 合成 `internal_only` 算子时显式使用）。三组都落到同一个 `CreateImpl`。

[src/ir/op_registry.cpp:238-257](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/op_registry.cpp#L238-L257) 的查表逻辑里有个贴心的分支：名字里**不含点号**时，报错追加「这看起来是函数名（GlobalVar），调用方应先检查 GlobalVar 再走 OpRegistry::Create」——把 2.1 节那两种被调者的混淆在报错里点破。紧接着是 internal_only 门禁：`entry.IsInternalOnly() && !allow_internal` 直接拒绝，用户代码**无法**通过拼出 `builtin.tensor.allreduce` 这个名字来越过 DSL 直接调内部算子。

**kwargs 校验**——按 schema 逐项对照：

[src/ir/op_registry.cpp:150-180](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/op_registry.cpp#L150-L180) 的 `ValidateKwargs` 对每个传入 kwarg 查 `allowed_kwargs`：查不到 → `ValueError`（未知 kwarg）；类型不符 → `TypeError`。有三个刻意的宽松/严格点值得注意：`DataType` 兼容 `int`（Python 侧历史习惯传 int）；`MemorySpace` 与 `TileLayout` 必须严格同型（不接受 int 替代）；其余类型逐一 `type_index` 精确比对。

**类型推断的异常处理**——保留异常类型与栈：

[src/ir/op_registry.cpp:270-286](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/op_registry.cpp#L270-L286) 用两个 catch 分支区分：PyPTO 自家异常走 `RethrowWithMessage`，**只追加位置、不改异常类型**（注释解释了为什么——把所有异常压平成 ValueError 会抹掉 CHECK/INTERNAL_CHECK 的区分）；非 PyPTO 异常（如 kwarg 类型错导致的 `std::bad_any_cast`）则折成 ValueError。`LocationSuffix` 把 span 渲染成 ` at <file>:<line>:<col>` 后缀。

**内存空间回填**——修 deducer 的遗漏 + 拒绝不可达输入：

[src/ir/op_registry.cpp:296-353](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/op_registry.cpp#L296-L353) 先做可达性检查（见下），再在 `deduce_output_memory` 存在时把空间回填到结果类型上：单输出直接替换 `TileType`；多输出（`TupleType`，如 `tile.gather_compare`）逐个补缺失的元素。注释指出这是为了修 issue #553——个别 deducer 忘写 `memory_space_`，由注册表层兜底。

可达性检查 [src/ir/op_registry.cpp:95-146](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/op_registry.cpp#L95-L146) 是本文件里最长的一段注释，核心思想可以用一句话概括：**只拦「任何后端都无法修复」的违法，其余留给 pass 17**。四种状态四种处理：空间未设置 → 合法（编译器稍后放置）；已设置且在允许集内 → 合法；不在允许集内但目标「从任一片上空间可达」→ 合法（pass 17 会插 `tile.move`）；不在允许集内且目标**没有任何入边** → 用户错误，立即拒绝。实践中最后一种几乎只有一个来源：约束为 `{Acc}`——L0C 只有 MAD 单元会写，没有 move 能把数据搬进去，累加器必须在 `Acc` 里被**生产出来**。报错文案还会针对「用 `tile.full` 预清零累加器」这一常见错误追加建议：改用 `init_cond`，让第一步覆写而不是累加（这正是 4.5 节的主题）。

#### 4.3.4 代码实践

**实践目标**：亲手触发 `CreateImpl` 的 ③（kwargs 校验）与 ②（internal-only 门禁）两条拒绝路径，观察报错差异。

**操作步骤**：

```python
from pypto.pypto_core import ir as _ir_core

# (a) 未注册的 kwarg → ValidateKwargs 的 ValueError
try:
    _ir_core.create_op_call("tile.load", [], {"bad_kwarg": 1},
                            _ir_core.Span.unknown())
except Exception as e:
    print("a:", type(e).__name__, e)

# (b) kwarg 类型不符 → TypeError
try:
    _ir_core.create_op_call("tile.load", [], {"clamp": "yes"},   # clamp 应为 bool
                            _ir_core.Span.unknown())
except Exception as e:
    print("b:", type(e).__name__, e)

# (c) internal-only 算子走用户面入口 → 被门禁拒绝
print("registered:", _ir_core.is_op_registered("builtin.tensor.allreduce"))
try:
    _ir_core.create_op_call("builtin.tensor.allreduce", [], {},
                            _ir_core.Span.unknown())
except Exception as e:
    print("c:", type(e).__name__, e)
```

注：`(a)`/`(b)` 传空 `args` 会在类型推断阶段先失败——若想精确落在 kwargs 校验，可改用一个参数个数正确但 kwarg 非法的调用；本实践以观察异常路径为目的，先后顺序不影响结论。

**需要观察的现象**：

- (a) 报 `Unknown kwarg 'bad_kwarg' for operator 'tile.load'`；
- (b) 报 kwarg 期望 bool 但类型不符；
- (c) 确认 `builtin.tensor.allreduce` **确实已注册**（`is_op_registered` 为 True），但通过 `create_op_call`（用户面）创建时被拒，报 internal-only 相关错误。

**预期结果**：三条异常都带清晰的上下文。(c) 是「注册了但用户不可达」的直接证据——`internal_only` 的守卫在**创建入口**而非使用入口。具体文案**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `CreateImpl` 不把「输入内存空间不符」全部拦下，而要区分「可达」与「不可达」？

**答案**：`Create` 不只服务用户编写路径——Pass 在半改写的 IR 上频繁调用它，此时某个操作数可能还是**合法化之前**的值（一个等待插 `tile.load` 的 GM 张量、一个后续阶段才会桥接的操作数）。若在这里拦截所有违例，会误伤这些瞬态。所以只拦「没有任何后端能修」的一类（`{Acc}` 无入边），其余交给运行在已定型 IR 上的 pass 17 处置。

**练习 2**：`python/bindings/modules/ir.cpp` 里为什么同时存在 `create_op_call` 与 `_create_internal_op_call` 两个函数？

**答案**：二者分别对应 `CreateUserFacing` 与 `CreateInternal`（[python/bindings/modules/ir.cpp:743-760](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/bindings/modules/ir.cpp#L743-L760) 与 [ir.cpp:771-779](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/bindings/modules/ir.cpp#L771-L779)）。下划线前缀标明后者**不是公共 API**，唯一调用方是往返解析器——它要重建打印机吐出的 `pl.builtin.<ns>.<op>(...)` 这种任何 DSL 包装都拼不出来的内部派发。这正是 u3-l1 讲过的打印-再解析往返的落地面。

---

### 4.4 按名字比较：IsOp 与名字身份不变量

#### 4.4.1 概念说明

Pass 与代码生成里随处可见「这个 Call 是不是 `tile.store`」这类判断。写这个判断有两种方式，一种对一种错：

```cpp
// ❌ 错：拼错静默变 false，bug 无声上线
if (call->op_->name_ == "tile.reshaep") { ... }

// ✅ 对：拼错当场抛 ValueError
if (IsOp(call, "tile.reshape")) { ... }
```

`IsOp` 的两个设计要点：

1. **字面量过 `GetOp`**——未注册的名字（拼错、改名）在比较点立刻报错，而不是静默返回 false；
2. **按规范名比较，不按指针**——这是 IR 维护的不变量。

#### 4.4.2 核心流程

```text
IsOp(call, "tile.reshape")
  └─ GetOp("tile.reshape")            ← 字面量验证：未注册 → ValueError
       └─ 取注册表单例里的规范 Op
  └─ call 非空 && call->op_->name_ == 规范名
```

为什么指针比较是错的：`Op` 实例在**好几处**被独立构造——注册表单例、`.pto` 反序列化器（`deserializer.cpp`）、MemRef 分配构造器（`memref_utils.h`）各自 `make_shared<Op>`。两个同名 `Op` 是**同一个算子**却是**两个不同指针**。

#### 4.4.3 源码精读

**IsOp 三连**：

[include/pypto/ir/op_registry.h:1109-1114](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/op_registry.h#L1109-L1114) 是核心实现：先无条件 `GetOp(op_name)`（注释点明「unconditionally so the guard fires even when `op` is null」——空指针也要先让字面量验证跑一遍），再 `op && op->name_ == canonical->name_`。[op_registry.h:1116-1119](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/op_registry.h#L1116-L1119) 与 [op_registry.h:1135-1139](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/op_registry.h#L1135-L1139) 分别是 `CallPtr` 与 `SubmitPtr` 的重载——注意 **Submit 也有**，这是 pass-submit-awareness 规则「凡处理 Call 处也要处理 Submit」在工具层的体现。

**LookupOpEntry 的 GlobalVar 守卫**：

[include/pypto/ir/op_registry.h:1121-1133](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/op_registry.h#L1121-L1133) 查条目之前先 `dynamic_pointer_cast<const GlobalVar>(op)` 排除函数引用。注释给出一个具体反例：`GlobalVar` 继承自 `Op`，一个函数调用带着**函数名**走到这里；若函数恰好叫 `tile.store`，按名字查表会把算子的副作用与复用契约错套到这个函数上——它的「参数 2」会被当成被写、被别名。**先看 kind，再看名字**。

**Python 侧的对应写法**：项目规则要求 Python 里也走 `get_op` 验证字面量，例如 `expr.op.name == _ir_core.get_op("array.get_element").name`；模块级的名字集合应从 `get_op(...).name` 构建而非裸字符串（import 期即验证每个字面量）。

#### 4.4.4 代码实践

**实践目标**：验证「同名不同指针」现象，并统计代码库里 `IsOp` 与裸名字比较的使用情况。

**操作步骤**：

1. 在 Python 里构造两个同名 `Op`，验证身份判定：

```python
from pypto.pypto_core import ir as _ir_core

a = _ir_core.get_op("tile.load")        # 注册表里的规范实例
b = _ir_core.Op("tile.load")            # 手工另造一个同名实例
print(a.name == b.name)   # True  —— 名字身份成立
print(a is b)             # False —— 指针身份不成立
```

2. 统计 C++ 侧两种写法的存量（仓库根目录）：

```bash
grep -rn "IsOp(" src/ include/ --include=*.cpp --include=*.h | wc -l
grep -rnE 'name_\s*==\s*"[a-z]+\.[a-z_]+"' src/ --include=*.cpp | wc -l
```

3. 挑一个 `IsOp` 使用点读上下文，例如 [src/ir/transforms/python_printer.cpp:1252-1253](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/python_printer.cpp#L1252-L1253)（打印机判定 `tensor.matmul_acc` / `tile.gemv_acc`）。

**需要观察的现象**：

- 第 1 步 `a.name == b.name` 为 True、`a is b` 为 False——同名即同算子，指针不唯一。
- 第 2 步 `IsOp` 的计数应远大于裸比较的计数（后者主要残留在少数豁免场景，见练习 2）。

**预期结果**：名字是算子的身份；任何「这是不是算子 X」的判断都必须既验证字面量又按名比较。具体计数**待本地验证**（随 HEAD 演进）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `IsOp(const OpPtr&, ...)` 在 `op` 为空时也要先调用 `GetOp`？

**答案**：`GetOp` 承担的是**字面量验证**职责。若短路写成 `op && op->name_ == GetOp(...)->name_`，则当 `op` 为空时整个表达式直接为 false，`GetOp` 根本不执行——一个拼错的名字就静默溜过去了。先无条件取规范实例，保证字面量错误在任何输入下都会炸。

**练习 2**：哪些「看起来像算子名比较」的代码**不应该**改成 `IsOp`？

**答案**：把名字当**数据**而非算子判定的场景，例如：按名查表（`GetEntry(name)`、`GetFunction(name)`）；命名空间前缀匹配（`name.find("tile.") == 0`，匹配的是一族而非一个算子，没有单一算子可 `GetOp`）；非算子名字（函数名、kwarg 键、dtype 字符串）；以及**构造**场景（把字面量喂给 `GetOp(...)` / `create_op_call(...)` 本身）。对这些做转换是误用。

---

### 4.5 init_cond 家族：为什么是操作数而不是 kwarg

#### 4.5.1 概念说明

`init_cond` 是「条件累加初始化」谓词：一个 BOOL 标量，为真时累加算子把累加器**覆写**为 `lhs @ rhs` 而不是累加。它是 split-K 里 `k == 0` 那一步的惯用法——第一块覆写、后续块累加，从此既不用预清零累加器，也不用把第一轮 K 从循环里剥出来。

关键设计问题：这个「可选的第四个参数」该注册成 **kwarg** 还是**操作数**？

答案是操作数，理由有二（都源自 2.2 节的对照表）：

1. **它可能依赖循环变量**。`k0 == 0` 是一个含归纳变量的比较表达式，每次迭代值都不同；kwarg 只能装编译期常量，装不下它。
2. **它必须进 use-def 链**。作为操作数，它像任何 SSA 值一样被 Pass 看到、替换、传播；作为 kwarg，它只是不参与数据流的元数据。

目前注册了 `init_cond` 的算子共四个：`tile.matmul_acc`、`tile.batch_matmul_acc`、`tile.gemv_acc`、`tensor.matmul_acc`。

#### 4.5.2 核心流程

从 DSL 到发射的完整链路：

```text
DSL:  acc = pl.tile.matmul_acc(acc, a, b, init_cond=(k0 == 0))
        │  Python 封装层把 init_cond 追加为第 4 个位置操作数
        ▼
create_op_call("tile.matmul_acc", [acc, lhs, rhs, k0==0], {}, span)
        │  CreateImpl：3 或 4 个操作数都合法
        ▼
DeduceTileMatMulAccType
  ├─ args.size() == 3 || == 4     ← 可选操作数
  └─ CheckMatmulInitCond(args, 3) ← 第 4 个必须是 BOOL 标量
        ▼
打印（python_printer）
  ├─ tile.matmul_acc → 位置打印（第 4 槽本来就空）
  └─ tensor.matmul_acc / tile.gemv_acc → 关键字打印 init_cond=...
        │   （它们的第 4 槽已被 a_trans / acc_phase 占用）
        ▼
发射（依谓词是否编译期已知）
  ├─ 缺省或字面 False → pto.tmatmul.acc ins(dst,lhs,rhs) outs(dst)
  ├─ 字面 True        → pto.tmatmul ins(lhs,rhs) outs(dst)
  └─ 运行期谓词        → scf.if cond { tmatmul } else { tmatmul.acc }
```

发射表来自 [docs/en/dev/ir/05-operators.md:300-306](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/ir/05-operators.md#L300-L306)。硬件本身用 MAD 的 Xt 寄存器第 63 位（`cmatrixInit`）表达这个语义，无需分支；`scf.if` 是虚拟指令层的表达。由于 `matmul_acc` 是原地算子（`set_output_reuses_input(0)`），两个分支写同一缓冲，`scf.if` 不产出任何值——Acc tile 上不会物化 phi。

#### 4.5.3 源码精读

**tile.matmul_acc 的注册**——四件套 + 内存规格 + 副作用 + 第四操作数：

[src/ir/op/tile_ops/matmul.cpp:429-449](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/op/tile_ops/matmul.cpp#L429-L449) 一次把 4.2 节的所有概念串起来：`add_argument` 登记四个参数（第四个就是 `init_cond`，描述里写明「Optional BOOL scalar; where it holds the accumulator is overwritten … (the split-K `k == 0` step)」）；`set_input_memory` 约束 acc 在 `Acc`、lhs 在 `Left`、rhs 在 `Right`；`set_output_memory(Acc)` 固定输出空间；`set_output_reuses_input(0)` 声明原地；`set_arg_effect(0, ArgEffect::ReadWrite)` 配注释「C += A@B reads the running sum it adds to」——这正是 4.2 节 `ValidateArgEffects` 会检查的那对声明。

对照同文件 [matmul.cpp:415-427](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/op/tile_ops/matmul.cpp#L415-L427) 的 `tile.matmul`：没有 acc 参数、没有 `set_output_reuses_input`、参数 0/1 无 ReadWrite——非原地版本不需要副作用声明。

**gemv_acc 的注册**——同一个谓词，多一个 kwarg：

[src/ir/op/tile_ops/matmul.cpp:482-503](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/op/tile_ops/matmul.cpp#L482-L503) 的 `tile.gemv_acc` 与 `matmul_acc` 几乎逐行对应（同样的 `init_cond` 第四操作数、`set_output_reuses_input(0)`、`set_arg_effect(0, ReadWrite)`），唯一多出 `set_attr<std::string>("acc_phase")`——一个真正的 kwarg，编译期常量，正好与 `init_cond` 形成对照。**这一条是本次增量窗口（PR #2528）新加的**：gemv_acc 此前没有 `init_cond`，现在与 matmul_acc 对齐。

**batch 与 tensor 层的注册**：

[src/ir/op/tile_ops/batch_matmul.cpp:295-317](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/op/tile_ops/batch_matmul.cpp#L295-L317) 的 `tile.batch_matmul_acc` 第四操作数描述更长：`FlattenTileNdTo2D` 展开成的每个 2D `tile.matmul_acc` 都会**逐字转发**这个谓词——每个展开体是自己那条行带的唯一写者，谓词按行带生效。[src/ir/op/tensor_ops/matmul.cpp:310-324](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/op/tensor_ops/matmul.cpp#L310-L324) 的 `tensor.matmul_acc` 同样登记第四操作数，并把 `a_trans`/`b_trans` 注册为 kwarg——又一次「值会变的做操作数、常量做 kwarg」。

**谓词的类型校验**：

[src/ir/op/type_inference.cpp:459-467](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/op/type_inference.cpp#L459-L467) 的 `CheckMatmulInitCond`：先 `As<ScalarType>` 确认是标量，再 `CHECK(dtype_ == DataType::BOOL)`。第二个 CHECK 的报错文案带着教学意图：`"Write a comparison such as 'k == 0' rather than passing the index itself."`——常见误用是把索引 `k0` 直接传进来（INT 标量）而非比较结果。

**Python 封装层如何落位**：

[python/pypto/ir/op/tile_ops.py:1824-1856](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/ir/op/tile_ops.py#L1824-L1856) 的 `matmul_acc` 把 `init_cond` 声明为 keyword-only 形参（`*, init_cond: Expr | None = None`），第 1855 行再决定落位：`args = [acc, lhs, rhs] if init_cond is None else [acc, lhs, rhs, init_cond]`。**Python 签名上的关键字只是人体工学**——进 IR 后它就是第四个位置操作数。docstring 里写明了动机：「keeps the accumulator single-def where a hand-written if/else would put a phi on an in-place Acc buffer」。

**打印层的两种形态**：

[src/ir/transforms/python_printer.cpp:1243-1253](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/python_printer.cpp#L1243-L1253) 的注释把差别讲得很清楚：`tile.matmul_acc` 的第 4 位置槽是空的，谓词直接位置打印；而 `tensor.matmul_acc`（第 4 槽被 `a_trans` 占）和 `tile.gemv_acc`（第 4 槽被 `acc_phase` 占）若位置打印，重解析时会绑到错误的形参上。于是 [python_printer.cpp:1263](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/python_printer.cpp#L1263) 跳过第 4 个操作数、[python_printer.cpp:1288-1292](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/python_printer.cpp#L1288-L1292) 以 `init_cond=` 关键字补打——每种打印形态重解析回同一 IR。这也是 u3-l1 讲过的「打印-再解析往返」在具体算子上的落点。

**权威文档的设计陈述**：

[docs/en/dev/ir/05-operators.md:286-289](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/ir/05-operators.md#L286-L289) 一句话给出完整理由：「The predicate is a positional operand rather than a registry kwarg because it may be loop-dependent; kwargs carry only compile-time constants. Registering it as an operand also means it participates in the use-def chain like any other SSA value.」

#### 4.5.4 代码实践

**实践目标**：从 IR 侧证明 `init_cond` 是操作数不是 kwarg，并复现「同一个操作数、三种打印形态」的往返一致性。

**操作步骤**：

1. 运行仓库里现成的往返测试（这是本实践最可靠的依据）：

```bash
source .claude/skills/testing/load-env.sh
python -m pytest tests/ut/ir/printing/test_python_printer.py::test_acc_init_cond_print_parse_roundtrip -v
```

2. 打开 [tests/ut/ir/printing/test_python_printer.py:1750-1810](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/ir/printing/test_python_printer.py#L1750-L1810) 读这个测试：它在 `@pl.program` 里定义三个 InCore 函数，分别用三种写法传同一个谓词 `k0 == 0`——

```python
# 摘自该测试（示例引用，非完整代码）
def tensor_acc(self, acc, lhs, rhs, k0) : return pl.tensor.matmul_acc(acc, lhs, rhs, init_cond=k0 == 0)
def tile_acc(self, acc, lhs, rhs, k0)   : return pl.tile.matmul_acc(acc, lhs, rhs, k0 == 0)
def tile_gemv_acc(self, acc, lhs, rhs, k0): return pl.tile.gemv_acc(acc, lhs, rhs, init_cond=k0 == 0)
```

   然后断言打印结果分别是：
   - `pl.tensor.matmul_acc(acc, lhs, rhs, init_cond=k0 == 0, a_trans=False, b_trans=False)`（关键字）
   - `pl.tile.matmul_acc(acc, lhs, rhs, k0 == 0)`（位置）
   - `pl.tile.gemv_acc(acc, lhs, rhs, init_cond=k0 == 0, acc_phase='unspecified')`（关键字）

   最后 `parse_program(printed)` 再解析并用 `assert_structural_equal` 验证与原程序结构相等。

3. 把测试中的谓词换成非法值复现类型校验：在 `tile_acc` 里把 `k0 == 0` 改成 `k0`（把索引本身传进去，INT 标量），重新运行，观察报错。

4. 顺手确认 DSL 层的 keyword-only 只是人体工学：读 [python/pypto/language/op/tile_ops.py:1292-1319](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/op/tile_ops.py#L1292-L1319) 的 `matmul_acc` 封装，注意它接收 `init_cond` 形参后调用的是 `_ir_ops.matmul_acc(..., init_cond=predicate_to_expr(init_cond))`——进了 IR 层就变成第 4 个位置操作数。

**需要观察的现象**：

- 第 1 步测试通过；第 2 步三种打印形态互不相同，但再解析后三者结构相等——「打印形态服从 DSL 签名、IR 形态始终是第 4 操作数」。
- 第 3 步报错要求 BOOL 标量，并建议「写一个比较表达式，别把索引本身传进来」。

**预期结果**：`init_cond` 在 IR 层始终是第 4 个位置操作数；两个第 4 槽被占用的签名（`tensor.matmul_acc` 的 `a_trans`、`tile.gemv_acc` 的 `acc_phase`）在打印层改用关键字以避免重解析错绑。第 1、2 步有现成测试背书；第 3 步的具体报错文案**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：如果把 `init_cond` 注册成 kwarg（`set_attr<bool>("init_cond")`），会在哪一步失败？

**答案**：在 `ValidateKwargs` 的**类型层**就装不下运行期谓词——`set_attr` 的 `static_assert` 只允许 bool/int/string/double/DataType/MemorySpace/TileLayout/PadValue 等**常量类型**（[include/pypto/ir/expr.h:114-125](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/expr.h#L114-L125)），`ExprPtr` 根本不在白名单里。就算类型能过，kwarg 不进 use-def 链，Pass 也无法对它做替换与传播——`k0 == 0` 里的 `k0` 会变成悬空引用。

**练习 2**：`tile.gemv_acc` 同时有 `acc_phase`（kwarg）和 `init_cond`（操作数）。用 2.2 节的判据说明这个分裂为什么是对的。

**答案**：`acc_phase` 是编译期就定死的模式选择（字符串常量，发射时直接决定指令属性），永远不依赖循环状态，所以放 kwarg；`init_cond` 的典型值 `k0 == 0` 随迭代变化，且需要作为 SSA 值参与数据流分析，所以放操作数。判据不是「可选与否」（二者都可选），而是「值会不会变、要不要进 use-def 链」。

**练习 3**：`tensor.matmul_acc` 的 rank>2 操作数会被 `ConvertTensorToTileOps` 派发到 `tile.batch_matmul_acc`，谓词如何跟着走？

**答案**：逐字转发。`FlattenTileNdTo2D` 把 batch 算子展开成若干 2D `tile.matmul_acc`，每个都带上同一个 `init_cond`——每个展开体是它那条累加器行带的唯一写者，谓词按行带逐一生效（见 [src/ir/op/tile_ops/batch_matmul.cpp:303-306](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/op/tile_ops/batch_matmul.cpp#L303-L306) 的参数描述与 [docs/en/dev/ir/05-operators.md:276-284](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/ir/05-operators.md#L276-L284)）。注意只有 `batch_count == 1`（如 `[1, N, K]` 的 grouped-GEMM）今天能到代码生成，更大的 batch 因 L0C 行窗口无法被 MAD 寻址而在 `FlattenTileNdTo2D` 被拒——与谓词无关。

---

## 5. 综合实践

**任务：为三个算子建立「档案卡对照表」，并用注册表数据解释一个编译期报错。**

1. **建档**：从 `tile.` 命名空间挑三个你熟悉的算子（建议 `tile.load`、`tile.matmul_acc`、`tile.store`），用本讲学过的查询手段各建一张卡：

   | 字段 | 获取方式 |
   | --- | --- |
   | kwarg schema | `_ir_core.get_op(name).get_attr_keys()` |
   | 内存规格 | `_ir_core.get_op_memory_spec(name)` |
   | 参数副作用 | `_ir_core.get_op_arg_effect(name, i, **kwargs)`，对每个下标各查一次 |
   | 写通道 | `_ir_core.get_op_write_channel(name)` |
   | 位置参数文档 | 读 C++ 注册处的 `add_argument` 描述（Python 绑定未暴露该项） |

2. **交叉验证**：把卡片上的每一项与 `src/ir/op/` 下对应的 `REGISTER_OP` 语句逐行对照，标出「绑定暴露的」与「只在 C++ 里能读到的」两类信息。

3. **解释报错**：构造一个「约束为 `{Acc}` 但值不在 Acc 里」的场景（例如给 `tile.matmul_acc` 的 acc 参数喂一个 Vec 里的 tile），触发 `CheckOperandMemorySpaceReachable` 的拒绝路径；先读报错文案，再回到 [src/ir/op_registry.cpp:95-146](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/op_registry.cpp#L95-L146) 找到产生这段文案的代码，用自己的话写三五行说明：为什么编译器不插一个 `tile.move` 来救？为什么建议改用 `init_cond`？

**完成标志**：三张卡片能互相解释差异（如 `tile.load` 的 deferred 输出 vs `tile.matmul_acc` 的固定 Acc 输出 vs `tile.store` 的 kwarg 依赖副作用）；报错解释能把「L0C 只有 MAD 单元会写」这条硬件事实与「累加器必须在 Acc 里被生产出来」这条结论连起来。第 3 步的具体报错触发方式**待本地验证**。

## 6. 本讲小结

- `OpRegistry` 是进程级单例，键为带命名空间的算子全名；`REGISTER_OP` 宏靠静态初始化把约 300 个算子（tile 138 / tensor 106 / pld 23 / system 22 / …）在 `import pypto` 之前注册完毕。
- 一条 `OpRegistryEntry` 携带五组信息：必备元数据（描述/类别/参数表/类型推断）、内存规格（输入约束 + 输出解析器 + 原地复用）、参数副作用（Read/Write/ReadWrite，可依 kwarg 而定）、跨核与亲和、internal-only 可见性。前四组缺项由 `GetOp()` 与两道 import 期校验兜底。
- `CreateImpl` 是建 `Call` 的唯一工厂：查表 → internal-only 门禁 → kwargs schema 校验 → `f_deduce_type` 推断（异常只追加 span 不改类型）→ 内存空间回填；输入空间只拦「任何后端都无法修复」的违例（实践中即 `{Acc}` 无入边）。
- 算子身份是**名字**不是指针：反序列化器与 MemRef 构造器都会另造同名 `Op`。`IsOp` 让字面量过 `GetOp`（拼错即报错）并按规范名比较，`Call`/`Submit` 都有重载；`LookupOpEntry` 先排除 `GlobalVar` 再按名查表。
- `init_cond` 注册为**可选的第四个位置操作数**而非 kwarg，因为它可依赖循环变量（`k0 == 0`）且必须进 use-def 链；kwarg 只装编译期常量。四个累加算子（`tile.matmul_acc` / `tile.batch_matmul_acc` / `tile.gemv_acc` / `tensor.matmul_acc`）统一采用此设计，`CheckMatmulInitCond` 强制其为 BOOL 标量；打印层在第 4 槽被占用的两个签名上改用关键字打印以保证往返一致。

## 7. 下一步学习建议

- **下一讲 u4-l7（IR 构建、打印与解析）**会用到本讲的 `create_op_call`：用 Builder API 手工构造 IR 时，每条算子调用都要经过 `CreateImpl` 的全部校验。届时你会更清楚地看到「打印成关键字、存成操作数」的往返细节。
- 想看内存规格如何被 Pass 消费，跳到 **u5-l6（Tile 后端降级链）**：pass 17 InferTileMemorySpace 正是 `deferred` 输出空间与 `HasRetargetableMemoryKwarg` 的消费者。
- 想看参数副作用如何变成依赖边，读 **u5-l7（内存规划三部曲）**：`ArgEffect::Write/ReadWrite` 决定生命周期分析与共享缓冲的拒绝条件。
- 若打算自己加算子，直接读 **u7-l8（新增算子的全栈流程）** 与 `.claude/skills/add-op/`——本讲的注册条目五组信息就是你要逐项填的清单，`init_cond` 是「新参数该做操作数还是 kwarg」的现成判例。
