# 从 DSLX 到 IR 的转换

## 1. 本讲目标

前两讲（u2-l3 类型推导、u3-l1 IR 总览）分别建立了 DSLX 的「带类型 AST」和 XLS 的「数据流 IR」两个世界。本讲要回答的核心问题是：**这两个世界之间是怎么连通的？** 也就是类型检查通过之后，DSLX 的 AST 节点是如何被「翻译（lowering，降级）」成 XLS IR 的 Node 的。

学完本讲，你应当能够：

- 说清从 `.x` 文件到 `Package` 的整条调用链：谁负责解析与类型检查、谁负责排序、谁负责逐节点翻译。
- 解释 `FunctionConverter` 用「访问者 + `node_to_ir_` 映射表」把 AST 节点变成 IR `BValue` 的核心机制。
- 看懂 `if` / `match` / `for` 这三类高层结构分别被 lowering 成了哪些 IR 运算符（`sel`、`priority_sel`、`counted_for`），并能对照真实生成的 `.ir` 文本验证。
- 知道 `ir_converter_main` 只是一个「薄壳」，真正干活的是库函数 `ConvertFilesToPackage`。

## 2. 前置知识

在进入源码前，先用一段话建立直觉。

DSLX 的 AST 是为「人」和「类型检查器」设计的——它保留了 `if`、`match`、`for`、`let` 这些高层、嵌套、带控制流的表达式。而 XLS IR 是为「硬件综合」设计的——它是**扁平的、单赋值（SSA）的数据流图**，没有嵌套，只有「算子（Op）吃若干输入、产出一个值」。所以转换的本质是**把带控制流与嵌套的树，拍扁成一张有向无环图**。

这里有几个关键词先记住：

- **lowering（降级）**：把一个高层结构用一组更底层的 IR 运算符表达出来。例如 `if` 没有「分支」语义，它会被 lowering 成一个多路选择器 `sel`。
- **BValue**：`FunctionBuilder` 在构造 IR 时返回的「构建期句柄」，每个 BValue 最终对应图里的一个 Node（详见 u3-l1）。
- **节点到值的映射**：转换器需要一张表，记录「这个 AST 节点翻译完之后，对应哪个 BValue」。后续节点引用它时，就查这张表。

还需要回顾两个事实（来自前置讲义）：

1. 到本讲为止，DSLX 源码**已经被解析成了 AST，并且类型检查已通过**——每个 AST 节点都已在 `TypeInfo` 里登记了类型（u2-l3）。本讲不再做类型推导，只做「照着类型信息把节点翻译成 IR」。
2. XLS IR 的图顶点是 `Node`，靠 `Op` 枚举区分语义（u3-l2），如 `kAdd`、`kEq`、`kSelect`、`kArrayIndex`。

## 3. 本讲源码地图

本讲涉及的关键文件都集中在 `xls/dslx/ir_convert/` 目录下：

| 文件 | 作用 |
| --- | --- |
| [ir_converter.h](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/ir_convert/ir_converter.h) / [.cc](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/ir_convert/ir_converter.cc) | **顶层驱动**：提供 `ConvertFilesToPackage`、`ConvertModuleToPackage`、`ConvertOneFunction` 等入口，串联「解析→类型检查→排序→逐函数转换」。 |
| [conversion_info.h](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/ir_convert/conversion_info.h) | **转换产物**：定义 `PackageConversionData`，即转换最终交出的「`Package` + 接口信息」。 |
| [convert_options.h](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/ir_convert/convert_options.h) | 定义 `ConvertOptions`，控制是否校验 IR、是否转换测试、是否发射位置信息等。 |
| [function_converter.h](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/ir_convert/function_converter.h) / [.cc](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/ir_convert/function_converter.cc) | **核心翻译引擎**：`FunctionConverter` 类逐节点地把一个 DSLX 函数翻译成 IR 函数。本讲绝大部分篇幅都在这里。 |
| [ir_converter_main.cc](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/ir_convert/ir_converter_main.cc) | 命令行入口（u1-l5 用过的 `ir_converter_main`），只是一个调用 `ConvertFilesToPackage` 的薄壳。 |
| `ir_converter_legacy_test.cc` 及 `testdata/*.ir` | 测试与黄金参考 IR，本讲借用其中的 `Conditional`、`Match` 用例作为真实 lowering 示例。 |

> 说明：`xls/ir/function_builder.h`/`.cc` 虽不在 `ir_convert` 目录，但 `FunctionConverter` 高度依赖它的 `Select`、`MatchTrue`、`CountedFor` 等构造方法，本讲会一并引用。

## 4. 核心概念与源码讲解

本讲按「从外到内」的顺序拆成四个最小模块：先看顶层驱动怎么把一个文件变成 Package（4.1），再看它最终交出的产物长什么样（4.2），然后深入到逐节点翻译的核心引擎 `FunctionConverter`（4.3），最后用 `if`/`match`/`for` 三个最典型的高层结构来验证这套引擎是如何工作的（4.4）。

### 4.1 顶层驱动：ir_converter（从文件到 Package）

#### 4.1.1 概念说明

`ir_converter.h`/`.cc` 是整个 DSLX→IR 转换的「调度中心」。它对外暴露一组层次清晰的 API：

- `ConvertFilesToPackage`：给一组 `.x` 文件路径，产出整个 `Package`（`ir_converter_main` 用的就是它）。
- `ConvertModuleToPackage`：给一个已解析的 `Module`，产出 `Package`。
- `ConvertOneFunction`：只转换模块里的某一个入口函数（命令行的 `-entry` 走这条）。

它们的关系是层层下沉的：`ConvertFilesToPackage` 负责「读文件 → 解析 → 类型检查」，然后把已类型检查的 `Module` 交给 `ConvertModuleIntoPackage`；后者再调用 `ConvertCallGraph` 按「调用图顺序」逐个函数翻译。

#### 4.1.2 核心流程

以最常用的 `ConvertFilesToPackage` 为例，整条链路可以画成：

```
路径(.x文件)
   │  读文件内容
   ▼
AddContentsToPackage          ← 解析 + 类型检查 的收口
   │  Scanner/Parser → Module
   │  TypecheckModule → TypecheckedModule（含 TypeInfo）
   ▼
ConvertOneFunctionIntoPackage 或 ConvertModuleIntoPackage
   │
   ▼
GetConversionRecords / GetOrder    ← 决定「先转哪个函数」
   │  （被调用者必须先于调用者转换）
   ▼
ConvertCallGraph                 ← 遍历顺序，逐函数转换
   │  对每条 ConversionRecord:
   ▼
ConvertOneFunctionInternal
   │  new 一个 FunctionConverter
   │  converter.HandleFunction(...) / HandleProcNextFunction(...)
   ▼
PackageConversionData { package, interface }
```

一个关键设计是**转换顺序**：因为 IR 里调用一个函数需要先有被调函数的定义，所以必须按调用图「被依赖者优先」的拓扑序来转换。这个排序由 `GetConversionRecords`/`GetOrder`（实现在 `extract_conversion_order.cc`）负责。

#### 4.1.3 源码精读

**入口与薄壳。** `ir_converter_main.cc` 的 `RealMain` 把命令行标志打包成 `ConvertOptions`，然后直接调用 `ConvertFilesToPackage`，最后把结果 `DumpIr()` 打到 stdout——确认了它确实只是个薄壳：

[ir_converter_main.cc:L155-L164](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/ir_convert/ir_converter_main.cc#L155-L164) —— 调用 `ConvertFilesToPackage` 拿到 `PackageConversionData result`，再 `result.package->DumpIr()` 输出 IR 文本。

**解析 + 类型检查的收口。** `AddContentsToPackage` 是「文本 → 可转换 Module」的必经之路：先用 `ParseText`（内含 `Scanner`+`Parser`）得到 `Module`，再 `TypecheckModule` 得到带类型的模块，最后才调用 `ConvertModuleIntoPackage`/`ConvertOneFunctionIntoPackage`：

[ir_converter.cc:L626-L666](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/ir_convert/ir_converter.cc#L626-L666) —— 注意 `TypecheckModule(std::move(module), ...)` 之后才进入转换；这印证了本讲的前提：**转换发生在类型检查之后**。

**按调用图顺序逐函数转换。** `ConvertCallGraph` 拿到排好序的 `order`，遍历每条 `ConversionRecord`，调用 `ConvertOneFunctionInternal`：

[ir_converter.cc:L286-L374](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/ir_convert/ir_converter.cc#L286-L374) —— 循环 `for (const ConversionRecord& record : order)` 里调用 `ConvertOneFunctionInternal`；结尾若 `options.verify_ir` 则 `VerifyPackage`。

**真正 new 出 FunctionConverter 的地方。** `ConvertOneFunctionInternal` 把外部依赖的常量挂上去，然后按记录类型分派给 `converter.HandleFunction` 或 `converter.HandleProcNextFunction`：

[ir_converter.cc:L185-L258](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/ir_convert/ir_converter.cc#L185-L258) —— 这里 `FunctionConverter converter(package_data, record.module(), import_data, options, ...)` 被构造（注释里称其为 "throwaway objects"，转完一个函数就丢弃），随后 `GetConstantDepFreevars` 收集函数体引用到的外部常量依赖并 `AddConstantDep`。

#### 4.1.4 代码实践

1. **实践目标**：从命令行侧验证「`ir_converter_main` 是 `ConvertFilesToPackage` 的薄壳」，并观察一个多函数模块的转换顺序。
2. **操作步骤**：
   - 准备一个含两个函数、且 `main` 调用 `helper` 的 `.x` 文件（例如 `helper` 做加法、`main` 调用它）。
   - 运行 `bazel build -c opt //xls/dslx/ir_convert:ir_converter_main`（u1-l2 已说明构建方式）。
   - 执行 `./bazel-bin/xls/dslx/ir_convert/ir_converter_main your_file.x`，查看 stdout 的 `.ir`。
   - 加上详细日志再跑一次：`./bazel-bin/xls/dslx/ir_convert/ir_converter_main --v=3 your_file.x 2>&1 | grep -i "conversion order\|converting to ir"`。
3. **需要观察的现象**：`.ir` 文本里 `helper` 对应的 `fn` 出现在 `main` 之前；`--v=3` 日志会打印 `Conversion order` 列表，`helper` 排在 `main` 之前。
4. **预期结果**：被调用函数的 IR 定义先于调用者出现，印证「调用图拓扑序」这一设计。
5. 若本机未配置 Bazel 环境，无法运行命令，则标注「待本地验证」，改为纯阅读型实践：在 `ir_converter.cc` 的 `ConvertCallGraph` 中确认循环顺序由 `order` 决定。

#### 4.1.5 小练习与答案

- **练习 1**：`ConvertFilesToPackage`、`ConvertModuleToPackage`、`ConvertOneFunction` 三者的输入粒度有什么不同？
  - **答案**：分别是「文件路径」「已解析的 Module」「模块内的某个具名函数」。前一个负责到后一个的逐步收窄；`ConvertFilesToPackage` 最外层还多做了读文件与类型检查。
- **练习 2**：为什么转换要按调用图拓扑序进行？
  - **答案**：IR 中函数调用节点（如 `invoke`）需要引用被调函数的定义，所以被依赖的函数必须先被转换并放入 `Package`。

---

### 4.2 转换产物：PackageConversionData 与 PackageData

#### 4.2.1 概念说明

转换的最终产物用一个极简的结构 `PackageConversionData` 表示。它只装两样东西：转换出来的 `Package`（IR 的顶层容器，u3-l1 已讲），以及一份描述对外接口的 proto `PackageInterfaceProto`。

而在转换**过程内部**，驱动层还维护着一个更丰满的 `PackageData`（定义在 `function_converter.h`），它额外记录「IR 函数 ↔ DSLX 函数」的对应关系、哪些是包装函数（wrapper）等元信息，供多个 `FunctionConverter` 实例共享。

#### 4.2.2 核心流程

```
PackageConversionData  (对外产物)
   ├── package            : unique_ptr<Package>   ← 整张 IR 图的容器
   └── interface          : PackageInterfaceProto  ← 端口/类型接口信息
        └── DumpIr()      : 直接调 package->DumpIr()

PackageData  (内部共享状态，函数级转换期间存在)
   ├── conversion_info    : PackageConversionData*  ← 指向上面的产物
   ├── ir_to_dslx         : IR函数 → DSLX函数 映射
   ├── wrappers           : 隐式 token 包装函数集合
   └── ...
```

#### 4.2.3 源码精读

**对外产物。** `PackageConversionData` 定义非常短，一眼能看到它就装了 `package` 和 `interface`，并提供 `DumpIr()` 便捷方法：

[conversion_info.h:L27-L34](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/ir_convert/conversion_info.h#L27-L34) —— `std::unique_ptr<Package> package;` 与 `PackageInterfaceProto interface;`，`DumpIr()` 直接转发给 `package->DumpIr()`。

**内部共享状态。** `PackageData` 把 `conversion_info` 与若干映射表打包在一起，由 `ConvertCallGraph` 持有，再以引用传给每个 `FunctionConverter`：

[function_converter.h:L93-L105](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/ir_convert/function_converter.h#L93-L105) —— 注意 `ir_to_dslx`（`xls::FunctionBase*` → `dslx::Function*`）和 `wrappers`（隐式 token 包装函数集合）。`ir_to_dslx` 在 `HandleFunction` 末尾被回填（见 4.3.3）。

**控制选项。** `ConvertOptions` 决定了转换过程的若干开关，例如 `verify_ir`（是否校验产物）、`convert_tests`（是否把 `#[test]` 也转成 IR）、`emit_positions`（是否把源码位置写进 IR）：

[convert_options.h:L29-L77](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/ir_convert/convert_options.h#L29-L77) —— 默认 `verify_ir=true`、`convert_tests=false`，这解释了为什么 `ir_converter_main` 默认不会输出测试函数的 IR。

#### 4.2.4 代码实践

1. **实践目标**：体会「同一个 Package 可以容纳多个函数」，并理解 `interface` 的作用。
2. **操作步骤**：对 4.1.4 的多函数 `.x` 运行 `ir_converter_main`，观察输出的 `package XXX` 块下有几个 `fn`。
3. **需要观察的现象**：一个 `package` 下有多个 `fn`，且文件开头的 `file_number` 行建立了文件名到编号的映射。
4. **预期结果**：`Package` 作为顶层容器，持有全部转换出来的函数（u3-l1 的「Package 容器」结论在这里被坐实）。
5. 本实践可在阅读 `.ir` 输出时完成，无需额外工具。

#### 4.2.5 小练习与答案

- **练习**：`PackageConversionData` 和 `PackageData` 各自的生命周期与用途有何区别？
  - **答案**：`PackageConversionData` 是对外返回的最终产物，转换结束后存活；`PackageData` 是转换过程中的内部共享状态（含 IR↔DSLX 映射等），仅在 `ConvertCallGraph` 执行期间存在，用于多个 `FunctionConverter` 协作。

---

### 4.3 function_converter：AST 节点到 IR Node 的映射引擎

#### 4.3.1 概念说明

`FunctionConverter` 是本讲的主角。注释里把它描述为「给定 DSLX AST、类型信息、入口函数，在一个 `Package` 内为它创建 IR 的助手」。它有一句很关键的设计自白：

> these are "throwaway objects"; i.e. instantiate them once to visit a single function and then discard.

也就是说，**每转换一个函数，就 new 一个 `FunctionConverter`，转完即弃**。这一点在 4.1.3 的 `ConvertOneFunctionInternal` 里已经看到了。

它最核心的状态是一张映射表 `node_to_ir_`：把「AST 节点指针」映射到「该节点翻译出的 IR 值」。后续节点要引用前序节点时，就查这张表。

#### 4.3.2 核心流程

`FunctionConverter` 的翻译模型可以概括为「**访问者驱动 + Def/Use 账本**」：

```
HandleFunction(f)
  │  1. 建一个 FunctionBuilder（IR 构造器）
  │  2. Visit 每个 Param → Def(name_def, builder.Param(...))
  │  3. 处理 parametric 绑定、外部常量依赖
  │  4. Visit(f.body())  ← 递归遍历函数体
  │       │
  │       ▼  对每个 AST 节点 n：
  │     FunctionConverterVisitor::HandleXxx(n)
  │       ├─ (部分类型) 先 VisitChildren(n)   ← 后序：先翻译子节点
  │       └─ converter_->HandleXxx(n)
  │             ├─ Use(child)   ← 查 node_to_ir_ 拿子节点的 BValue
  │             ├─ builder->SomeOp(...) ← 造新 IR 节点
  │             └─ Def(n, result) ← 把结果写回 node_to_ir_[n]
  │  5. Use(f.body()) 拿返回值，BuildWithReturnValue 完成 IR 函数
  ▼
Package 里多了一个 xls::Function
```

**三种遍历策略。** 访问者 `FunctionConverterVisitor` 用三个宏把 AST 节点分成三类处理方式，这是理解「谁先翻译子节点」的关键：

- `TRAVERSE_DISPATCH(Xxx)`：先 `VisitChildren`（后序遍历子节点），再调 `HandleXxx`。用于「叶子值都来自子节点」的简单节点（如 `Binop`、`Unop`、`XlsTuple`）。
- `NO_TRAVERSE_DISPATCH(Xxx)`：**不**自动遍历子节点，全部由 `HandleXxx` 自己决定怎么访问。用于 `For`、`Match`、`Conditional`、`Invocation` 这类需要精细控制翻译顺序的高层结构——本讲 4.4 的主角都属于此类。
- `INVALID(Xxx)`：不应该在函数体内出现的节点（如 `Module`、`Import`、类型注解），遇到就报错。

**Def / Use 账本。** 这对操作是整个映射机制的枢纽：

- `Def(node, ir_func)`：执行 `ir_func` 造一个 IR 节点，把结果登记到 `node_to_ir_[node]`。
- `Use(node)`：查 `node_to_ir_[node]`，返回先前登记的 `BValue`。
- `DefAlias(from, to)`：让 `to` 节点复用 `from` 已有的 IR 值（用于 `let`、参数名、外部常量引用等「只是换了个名字」的场景）。

#### 4.3.3 源码精读

**IrValue：AST 节点能映射到的所有可能形态。** 大多数 AST 节点映射到 `BValue`（普通 IR 节点句柄）或 `CValue`（带编译期常量的句柄），而 Proc 相关的还会映射到 `Channel*`、`ChannelInterface*` 等：

[function_converter.h:L253-L255](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/ir_convert/function_converter.h#L253-L255) —— `using IrValue = std::variant<BValue, CValue, Channel*, ChannelInterface*, ChannelArray*, ProcDefInstance*, xls::StateElement*>;`

**核心映射表。** `node_to_ir_` 就是那张「AST 节点 → IrValue」的账本：

[function_converter.h:L665](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/ir_convert/function_converter.h#L665) —— `absl::flat_hash_map<const AstNode*, IrValue> node_to_ir_;`

**访问者的三种分派宏。** 这段是「翻译顺序」的总开关：

[function_converter.cc:L350-L354](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/ir_convert/function_converter.cc#L350-L354) —— `TRAVERSE_DISPATCH` 宏：先 `VisitChildren(node)`，再 `converter_->HandleXxx(node)`。

[function_converter.cc:L367-L370](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/ir_convert/function_converter.cc#L367-L370) —— `NO_TRAVERSE_DISPATCH` 宏：直接调 `converter_->HandleXxx(node)`，不自动遍历子节点。注意随后列表里出现了 `Conditional`、`For`、`Match`、`Invocation`、`Let`——这些都是需要自定义翻译顺序的高层结构。

**Def 账本写入。** `Def`/`DefWithStatus` 先用 `ir_func` 造节点，再 `SetNodeToIr` 登记：

[function_converter.cc:L610-L649](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/ir_convert/function_converter.cc#L610-L649) —— `DefWithStatus` 调用 `ir_func(loc)` 得到 `BValue result`，检查 builder 无错后 `SetNodeToIr(node, result)`；`Def` 是它「不可失败」的包装；`DefConst` 额外用 `Literal` 把编译期常量装进 `CValue`。

**Use 账本读取。** `Use` 查表并从 `IrValue` 里取出 `BValue`：

[function_converter.cc:L651-L669](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/ir_convert/function_converter.cc#L651-L669) —— `Use` 先 `GetNodeToIr(node)`，再根据 `IrValue` 是 `BValue` 还是 `CValue` 返回对应的 `.value`。

**DefAlias：换名即复用。** `let x = ...`、参数名、外部常量引用都不需要造新 IR 节点，只需让新名字指向旧值，并顺手给 IR 节点改个可读名字：

[function_converter.cc:L570-L608](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/ir_convert/function_converter.cc#L570-L608) —— `DefAlias(from, to)` 把 `node_to_ir_[from]` 复制给 `node_to_ir_[to]`；若 `to` 是 `NameDef`，还会给对应 IR 节点 `SetName(identifier)`（这就是为什么生成的 `.ir` 里有可读的名字而不是全用 `add.3` 这种自动名）。

**参数翻译示例。** `HandleParam` 把一个 DSLX 参数翻译成 IR 的 `Param` 节点，是「Def 账本」最直白的用法：

[function_converter.cc:L856-L874](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/ir_convert/function_converter.cc#L856-L874) —— `Def(node->name_def(), [&](loc){ return function_builder_->Param(node->identifier(), type); });`，同时把参数类型写进 `function_proto`。

**函数级总装。** `HandleFunction` 把上述零件串起来：建 builder → 处理参数与 parametric 绑定 → 翻译外部常量依赖 → `Visit(f.body())` → `Use(body)` 取返回值 → `BuildWithReturnValue`：

[function_converter.cc:L3482-L3626](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/ir_convert/function_converter.cc#L3482-L3626) —— 注意 L3524 先 `Visit` 每个 `Param`，L3571 `Visit(f.body())` 翻译函数体，L3585 `BuildWithReturnValue(return_value)` 最终把 builder 里累积的节点固化成一个 `xls::Function`，L3624 `package_data_.ir_to_dslx[ir_fn] = node;` 回填映射。

#### 4.3.4 代码实践

1. **实践目标**：亲手追踪一个 `Binop`（如 `a + b`）走完「后序遍历子节点 → Def/Use」的完整账本流转。
2. **操作步骤**：
   - 在 [function_converter.cc:L356-L363](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/ir_convert/function_converter.cc#L356-L363) 确认 `Binop` 走的是 `TRAVERSE_DISPATCH`（先 `VisitChildren`，再 `HandleBinop`）。
   - 阅读尾部的 `HandleBinop`（[function_converter.cc:L4716](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/ir_convert/function_converter.cc#L4716) 起）：它对左右子节点 `Use(...)`，再调用 builder 造对应运算节点并 `Def`。
   - 写一个 `fn add(a: u8, b: u8) -> u8 { a + b }`，用 `ir_converter_main` 转换，对照输出里的 `add` 节点。
3. **需要观察的现象**：输出的 `.ir` 里有一个 `add.3: bits[8] = add(param_a, param_b)` 之类的节点，参数节点先于它出现。
4. **预期结果**：印证「子节点先翻译并登记，父节点 `Use` 子节点结果再造新节点」的后序模型。
5. 命令运行需要 Bazel 环境；若不可用则改为纯阅读：在 `HandleBinop` 中确认它先 `Use(lhs_node)`/`Use(rhs_node)` 再 `Def`。

#### 4.3.5 小练习与答案

- **练习 1**：为什么 `Match`、`For`、`Conditional` 用 `NO_TRAVERSE_DISPATCH` 而不是 `TRAVERSE_DISPATCH`？
  - **答案**：因为它们需要自定义子节点的翻译顺序与条件控制谓词（control predicate）。例如 `Conditional` 必须先翻译条件、再在「分支被激活」的谓词上下文里翻译两个分支；若用自动后序遍历，就无法在正确的控制谓词下访问分支体。
- **练习 2**：`DefAlias` 在什么场景下被使用？它对最终 `.ir` 文本有什么可见影响？
  - **答案**：用于「换名不造新节点」的场景，如 `let` 绑定、参数别名、外部常量引用。可见影响是让 IR 节点带上可读名字（通过 `SetName`），而不是只剩 `add.3` 这种自动名。

---

### 4.4 高层结构 lowering：if / match / for

有了 4.3 的映射引擎，现在用它来解释三个最典型的高层结构是如何被「拍扁」成 IR 的。本模块是理解 DSLX→IR 转换的关键，也是本讲代码实践的主战场。

#### 4.4.1 概念说明

XLS IR 没有真正的「分支」「循环」语义——它是数据流图。所以 DSLX 的高层结构必须 lowering 成等价的数据流运算：

| DSLX 结构 | lowering 产物（核心 IR 运算符） | 直觉 |
| --- | --- | --- |
| `if c { then } else { else_ }` | `sel(c, cases=[else_, then])`（二元 `kSelect`） | 二选一多路选择器 |
| `match x { p1=>v1, p2=>v2, _=>vd }` | 一组 `eq`（每个非默认 arm 一个）→ `concat` → `priority_sel(..., default=vd)` | 优先级选择器 |
| `for (i, acc) in range { body }` | 一个独立的「循环体函数」+ `counted_for(init, trip_count, body_fn)` | 把循环体抽成函数，再展开调用 |

注意几个要点：

- **`if` 的条件若是编译期常量**，则只翻译命中的那个分支（constexpr 优化），不产生 `sel`。
- **`match` 的最后一个 arm 充当 default**：源码注释明确「穷尽性检查保证最后一个 arm 覆盖其余情况」，所以它被放在 `priority_sel` 的 `default=` 里，而不产生比较器。
- **`for` 的循环体被抽成一个独立 IR 函数**（名字形如 `__<函数名>_counted_for_<N>_body`），由 `counted_for` 节点按次数反复调用。这是「不可变累加器」语义（u1-l4）在 IR 层的落点。

#### 4.4.2 核心流程

**`if`（Conditional）流程：**

```
HandleConditional(node):
  if node->IsConst():          # 编译期可定条件
      只 Visit 命中的分支 → DefAlias 到 node   # 不产生 sel
      return
  Visit(test)  → arg0 = Use(test)
  在「arg0 为真」的控制谓词下 Visit(consequent) → arg1
  在「arg0 为假」的控制谓词下 Visit(alternate) → arg2
  Def(node, Select(arg0, arg1, arg2))   # 产生 sel 节点
```

**`match` 流程：**

```
HandleMatch(node):
  if node->IsConst():  → HandleConstMatch（只翻译选中 arm）   return
  Visit(matched) → matched = Use(matched)
  for 每个「非最后」的 arm:
      selector = HandleMatcher(模式, matched)   # 多半产出 eq(...)
      arm_selectors.push(selector)
      在「本 arm 被选中」的控制谓词下 Visit(arm.expr) → arm_values.push(...)
  最后一个 arm 作为 default: Visit(default.expr) → default_value
  result = MatchTrue(arm_selectors, arm_values, default_value)
       # 内部: reverse(selectors) → concat → PrioritySelect(...)
  SetNodeToIr(node, result)
```

**`for` 流程（简化）：**

```
HandleFor(node):
  Visit(init)
  求出 trip_count（从 range 或数组大小）
  new 一个 body_converter + 一个 FunctionBuilder(body_fn_name)   # 循环体独立函数
  为循环体加参数：归纳变量 ivar、累加器 carry、(若迭代数组) __indexable
  HandleForBody: 把循环体 block 在 body_converter 里翻译成 IR
  ir_body_function = body_builder.Build()
  Def(node, CountedFor(init, trip_count, stride=1, ir_body_function, invariant_args))
```

#### 4.4.3 源码精读

**`if` → `sel`。** `HandleConditional` 在非 constexpr 情况下，依次访问条件、两个分支，最后用 `Select(arg0, arg1, arg2)` 产生 `kSelect` 节点：

[function_converter.cc:L4963-L5011](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/ir_convert/function_converter.cc#L4963-L5011) —— L4979 `Visit(node->test())`，L4992/L5005 分别 `Use(consequent)`/`Use(alternate)`，L5007 `Def(node, [...]{ return function_builder_->Select(arg0, arg1, arg2, loc); })`。注意 L4983/L4995 用 `ScopedControlPredicate` 给两个分支套上「激活条件」，这是 `fail!`/`assert!` 在分支内能正确工作的基础。

> 一个容易踩的细节：这里调用的是**二元** `Select(selector, on_true, on_false)` 重载，而该重载内部会把参数重排成 `Select(selector, {on_false, on_true})`（见下方 builder 源码）。所以最终 `.ir` 里 `sel(c, cases=[A, B])` 的 `A` 是「假分支」、`B` 是「真分支」。这在下面的真实示例里会看得一清二楚。

**二元 Select 的参数重排。** 这解释了上面那个「踩坑点」：

[function_builder.cc:L275-L279](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/function_builder.cc#L275-L279) —— `Select(selector, on_true, on_false)` 内部调用 `Select(selector, {on_false, on_true}, ...)`。头文件里对此甚至留了一条 `TODO` 想统一参数顺序。

**`match` → 一组 `eq` + `priority_sel`。** `HandleMatch` 为每个非默认 arm 生成选择器（`HandleMatcher` 多半产出 `eq`），把最后一个 arm 当 default，最后调用 `MatchTrue`：

[function_converter.cc:L1322-L1449](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/ir_convert/function_converter.cc#L1322-L1449) —— L1342 循环 `i < arms().size()-1` 跳过最后一个 arm；L1444 `MatchTrue(arm_selectors, arm_values, default_value)`。L1417 `default_arm = node->arms().back()` 与 L1423-L1433 的注释（`selectors:[x==42,x==64,x==128]; values:[a,b,c]; default:d`）把整个 lowering 思路写得很清楚。constexpr 情况走 L1331 的 `HandleConstMatch`（[L1314-L1320](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/ir_convert/function_converter.cc#L1314-L1320)）。

**`MatchTrue` 的内部 = 反转 + concat + PrioritySelect。** 这一步把若干「单 bit 选择器」拼成一个优先级选择器的 selector：

[function_builder.cc:L390-L418](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/function_builder.cc#L390-L418) —— L414 `std::reverse(selector_bits...)`，L416 `Concat(selector_bits)`，L417 `return PrioritySelect(concat, case_values, default_value, ...)`。这就是为什么最终 `.ir` 里会出现 `concat` 和 `priority_sel`。

**`for` → 循环体函数 + `counted_for`。** `HandleFor` 新建一个 `body_converter` 和专门的 `FunctionBuilder`（循环体函数），把循环体翻译进去，再用 `CountedFor` 调用它：

[function_converter.cc:L1784-L1893](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/ir_convert/function_converter.cc#L1784-L1893) —— L1814 new `body_converter`，L1829 构造 `body_fn_name = "__<fn>_counted_for_<N>_body"`，L1872 `ir_body_builder_ptr->Build()` 得到循环体函数，L1881 `function_builder_->CountedFor(init, trip_count, /*stride=*/1, ir_body_function, invariant_args)`。注释 L1801-L1811 说明它把 `(ivar, accum)` 二元模式拆成归纳变量与累加器。

#### 4.4.4 代码实践（本讲主实践）

这是本讲的核心实践：**在 `function_converter` 中跟踪 `if`/`match` 如何被翻译成 `sel`/`priority_sel`，并用 `ir_converter_main` 生成 IR 对照确认。** 我们直接借用 XLS 自带的真实测试用例，确保结论可复现。

**第一步：理解源 DSLX（真实测试用例）。** 这两个用例来自 `ir_converter_legacy_test.cc`：

- `if` 用例 [ir_converter_legacy_test.cc:L871-L880](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/ir_convert/ir_converter_legacy_test.cc#L871-L880)：`fn main(x: bool) -> u8 { if x { u8:42 } else { u8:24 } }`
- `match` 用例 [ir_converter_legacy_test.cc:L728-L742](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/ir_convert/ir_converter_legacy_test.cc#L728-L742)：
  ```dslx
  fn f(x: u8) -> u2 {
    match x {
      u8:42 => u2:0,
      u8:64 => u2:1,
      _ => u2:2
    }
  }
  ```

**第二步：对照真实黄金 IR（仓库内已固化）。** 这两个用例的期望输出就在 `testdata/` 下，可作为「答案」：

- `if` 的 IR（[testdata/ir_converter_legacy_test_Conditional.ir:L5-L9](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/ir_convert/testdata/ir_converter_legacy_test_Conditional.ir#L5-L9)）：
  ```
  fn __test_module__main(x: bits[1] id=1) -> bits[8] {
    literal.3: bits[8] = literal(value=24, id=3)      // alternate (假分支) = 24
    literal.2: bits[8] = literal(value=42, id=2)      // consequent (真分支) = 42
    ret sel.4: bits[8] = sel(x, cases=[literal.3, literal.2], id=4)
  }
  ```
  注意 `cases=[literal.3(24), literal.2(42)]`：第一个是假分支、第二个是真分支——正好印证了 4.4.3 说的「二元 Select 参数重排」。

- `match` 的 IR（[testdata/ir_converter_legacy_test_Match.ir:L5-L16](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/ir_convert/testdata/ir_converter_legacy_test_Match.ir#L5-L16)）：
  ```
  fn __test_module__f(x: bits[8] id=1) -> bits[2] {
    literal.5: bits[8] = literal(value=64, id=5)
    literal.2: bits[8] = literal(value=42, id=2)
    eq.6: bits[1] = eq(literal.5, x, id=6)        // x == 64
    eq.3: bits[1] = eq(literal.2, x, id=3)        // x == 42
    concat.10: bits[2] = concat(eq.6, eq.3, id=10)
    literal.4: bits[2] = literal(value=0, id=4)   // arm0 的值
    literal.7: bits[2] = literal(value=1, id=7)   // arm1 的值
    literal.9: bits[2] = literal(value=2, id=9)   // default 的值
    literal.8: bits[1] = literal(value=1, id=8)
    ret priority_sel.11: bits[2] = priority_sel(concat.10, cases=[literal.4, literal.7], default=literal.9, id=11)
  }
  ```
  可以清楚看到：每个非默认 arm 产出一个 `eq`；两个 `eq` 经 `concat` 拼成 selector；`_ => 2` 没有比较器，直接成为 `priority_sel` 的 `default=`。

**第三步：自己复现。**
1. 实践目标：把上面两段 DSLX 用 `ir_converter_main` 转成 IR，验证与黄金 `.ir` 一致，并能解释每个节点的来历。
2. 操作步骤：
   - 把 `if` 与 `match` 两段 DSLX 分别存成 `cond.x`、`match.x`。
   - 运行 `./bazel-bin/xls/dslx/ir_convert/ir_converter_main cond.x` 与 `... match.x`。
   - 把输出与上面的黄金 IR 逐节点比对。
3. 需要观察的现象：`cond.x` 输出含一个 `sel`；`match.x` 输出含两个 `eq`、一个 `concat`、一个 `priority_sel`，且 `_` 分支不出 `eq`。
4. 预期结果：与 `testdata/` 下两个 `.ir` 文件完全一致（忽略 `id=` 编号可能的差异）。
5. 若本机无 Bazel 环境：改为纯阅读型实践——在 `HandleConditional`（[L4963](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/ir_convert/function_converter.cc#L4963)）与 `HandleMatch`（[L1322](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/ir_convert/function_converter.cc#L1322)）中手动标注「这一行对应黄金 IR 的哪个节点」，并据此填写下表（待本地验证）：

   | DSLX 结构 | 对应 handler 关键行 | 产出的 IR 节点 |
   | --- | --- | --- |
   | `if x {...} else {...}` | `Select(arg0,arg1,arg2)` @ L5008 | `sel` |
   | `match` 的每个非默认 arm | `HandleMatcher` @ L1348 | `eq` |
   | `match` 的选择器合并 | `MatchTrue` @ L1444 → `PrioritySelect` @ builder L417 | `concat` + `priority_sel` |
   | `match` 的最后一个 arm | `default_value` @ L1434 | `priority_sel` 的 `default=` |

#### 4.4.5 小练习与答案

- **练习 1**：如果把 `match` 用例里的 `_ => u2:2` 改成 `u8:200 => u2:2`（即不给通配兜底，但穷尽），转换结果会有什么不同？
  - **答案**：最后一个 arm 不再是 default，于是会多出一个 `eq(x, 200)`，且不再有 `default=`（或由穷尽性保证的等价处理）。本讲示例里 `_` 之所以没有比较器，正是因为它被当作 default。具体形式「待本地验证」。
- **练习 2**：`for` 循环为什么要把循环体抽成一个独立的 IR 函数，再用 `counted_for` 调用，而不是直接展开成一大片节点？
  - **答案**：把循环体抽成函数让 IR 图保持紧凑、可复用，并让后续优化与调度（u4）能以「函数调用 N 次」的语义来处理流水线展开；`counted_for` 节点显式携带 `trip_count` 与循环体函数指针，是这种「参数化重复」的天然表达。
- **练习 3**：`if` 的条件是编译期常量（如 `if true {...}`）时，`HandleConditional` 会怎样？
  - **答案**：走 [L4965-L4977](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/ir_convert/function_converter.cc#L4965-L4977) 的 constexpr 分支：用 `ConstexprEvaluator` 求出条件值，只 `Visit` 命中的那个分支并 `VisitAndPropagateIrValue` 到 `node`，**不产生 `sel` 节点**。

## 5. 综合实践

设计一个能把本讲四个模块串起来的小任务：**自己写一个含 `if`、`match`、`for` 的小 DSLX 函数，预测它的 IR 形态，再用 `ir_converter_main` 验证。**

建议函数（综合了三种结构）：

```dslx
// 示例代码：综合实践用，非项目原有文件
fn classify_and_sum(x: u8, n: u3) -> u8 {
  // 结构1: if -> sel
  let base = if x == u8:0 { u8:1 } else { x };
  // 结构2: match -> priority_sel
  let tag = match x {
    u8:42 => u8:10,
    u8:64 => u8:20,
    _ => base,
  };
  // 结构3: for -> counted_for，累加器为元组
  let result: u8 = for (i, acc) in u3:0..n {
    acc + tag
  }(base);
  result
}
```

任务步骤：

1. **预测**：在纸上画出预期 IR——`base` 处应有 `eq`+`sel`；`tag` 处应有 `eq`、`concat`、`priority_sel`（注意 `base` 作为 default 分支，会被复用而非重新计算）；`result` 处应出现一个独立的 `__classify_and_sum_counted_for_0_body` 函数与一个 `counted_for` 节点。
2. **验证**：用 `ir_converter_main` 转换，对照输出。重点关注：
   - 三个结构各自落到 4.4 表格预测的运算符上；
   - `let` 绑定是否如 4.3 所述通过 `DefAlias` 让节点带上 `base`/`tag`/`result` 这类可读名字；
   - 循环体是否真的被抽成了单独的 `fn`。
3. **反思**：试着把 `if` 条件改成编译期常量（如 `if true`），观察 `sel` 是否消失（呼应 4.4.5 练习 3）。

> 命令需要 Bazel 构建出的 `ir_converter_main`（见 u1-l2/u1-l5）。若环境不可用，本实践可降级为「阅读型」：在 `function_converter.cc` 的对应 handler 里逐行标注这段 DSLX 会命中哪段代码，并写出预期 IR 文本，标注「待本地验证」。

## 6. 本讲小结

- DSLX→IR 转换发生在**类型检查之后**：`ir_converter.cc` 的 `AddContentsToPackage` 先 `TypecheckModule`，再进入转换。
- 顶层驱动按**调用图拓扑序**逐函数转换（`ConvertCallGraph` → `ConvertOneFunctionInternal`），每转换一个函数就 new 一个一次性的 `FunctionConverter`。
- 转换的最终产物是极简的 `PackageConversionData`（`package` + `interface`），过程中的共享状态放在 `PackageData`。
- `FunctionConverter` 的核心是「**访问者 + `node_to_ir_` 账本**」：`Def` 写、`Use` 读、`DefAlias` 换名；`TRAVERSE_DISPATCH` 自动后序遍历子节点，`NO_TRAVERSE_DISPATCH` 留给需要自定义顺序的高层结构。
- 高层结构被 lowering 成数据流运算符：`if`→`sel`（constexpr 条件则只留命中分支）、`match`→一组 `eq`+`concat`+`priority_sel`（最后 arm 当 default）、`for`→独立的循环体函数 + `counted_for`。
- `ir_converter_main` 只是 `ConvertFilesToPackage` 的薄壳——这与 u1-l5 对工具链「薄壳」的判断一致。

## 7. 下一步学习建议

- **横向**：本讲只覆盖了 `Function` 的转换。`Proc`（有状态、带 `config`/`next`、带 channel）的转换走 `HandleProcNextFunction`、`ConvertProcDef` 与 `proc_config_ir_converter`，建议在学完 u3-l5（Proc/Channel）后回看 `function_converter.cc` 里这些 proc 相关 handler，以及 `channel_scope.h`。
- **纵向（向后）**：转换产出的 IR 接下来要进入优化阶段。建议进入 u4-l1（优化 Pass 框架），理解本讲产出的 `sel`/`priority_sel`/`counted_for` 等节点如何被后续 Pass（如 `arith_simplification`、`dce`、`unrolling`）改写。
- **动手**：如果想更扎实地理解 lowering，可以在 `function_converter.cc` 的 `HandleConditional`/`HandleMatch`/`HandleFor` 里临时加 `VLOG` 或断点，对照一个真实 `.x` 文件观察 `node_to_ir_` 在每一步的变化（只读实验，不要提交对源码的修改）。
