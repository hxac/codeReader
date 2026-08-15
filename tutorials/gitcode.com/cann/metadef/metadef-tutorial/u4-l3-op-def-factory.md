# OpDefFactory 与 OpDefRegistry：算子原型的注册与获取

## 1. 本讲目标

上一讲（u4-l2）我们学会了用 `ops::OpDef` 的链式 API 定义一个算子原型，但只停留在「定义与存储」。本讲回答下一个自然的问题：**这些 OpDef 是怎么被框架"发现"的？**

学完本讲你应该能够：

1. 说出 `OP_ADD` 宏展开后发生了什么：静态对象在 so 加载期把「算子名的创建函数」登记进 `OpDefFactory` 单例。
2. 画出「算子注册 → 工厂存储 → 按 opType 查询」的完整时序。
3. 指出工厂的线程安全策略：**全程无锁**，安全性来自 Meyers 单例的初始化保证 + 注册只发生在 so 静态初始化阶段。
4. 理解 `OpDefRegisterV2` 的 weak 符号设计与 `OP_PROTO_LIB` / `OP_TILING_LIB` 两种特殊编译形态。
5. 了解旁路的 `OpConfigRegistry`：SoC 级 `OpAICoreConfig` 如何用同样的「静态对象构造期注册」模式登记。

## 2. 前置知识

- **静态对象构造期注册**：u4-l1 已见过一次（`REGISTER_CUSTOM_OP` 宏生成静态 `OpReceiver`）。C++ 规定：一个 `.so` 被 `dlopen` 加载时，加载器会（单线程地）执行其中所有静态全局对象的构造函数。把"注册动作"写进静态对象的初始化表达式，就得到了「库一被加载、内容自动登记」的效果。这是本讲反复出现的核心机制。
- **Meyers 单例**：把 `static` 局部变量放在 `GetInstance()` 函数里。C++11 起标准保证这个变量的初始化是线程安全的（俗称 magic statics）：多线程同时首次调用 `GetInstance()`，只有一个线程执行构造，其余线程等待。
- **weak 符号**：`__attribute__((weak))` 标记的函数声明允许"没有实现"（链接后地址为 `nullptr`），也允许被别处的**强符号**同名定义覆盖。metadef 用它让注册入口在"未链接实现"或"被生成器替换"两种场景下都能工作。
- **`std::function` 与裸函数指针**：`OpDefCreator` 是 `std::function<OpDef(const char *)>`（可捕获 lambda），`OpDefFuncPtr` 是纯函数指针 `OpDef(*)(const char *)`（无捕获 lambda 可退化为它）。后者更"POD"、跨边界更安全，这是 V2 版本的改进动机之一。
- **pimpl 薄壳**：u4-l2 讲过 `OpDef` 对外只持 `unique_ptr<Impl>`。因此工厂里存"如何造一个 OpDef"的**创建函数**、按需现造，而不是长期缓存 OpDef 对象本身。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [inc/external/asc/register/op_def_factory.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/asc/register/op_def_factory.h) | 工厂**门面**：对外只暴露两个注册接口，查询接口私有、仅友元可达 |
| [inc/external/asc/register/op_def_registry.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/asc/register/op_def_registry.h) | `OP_ADD` 宏：算子作者实际书写的一行代码，按编译宏分三种形态 |
| [base/asc/opdef/op_def_factory.cc](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/asc/opdef/op_def_factory.cc) | 门面的实现：逐行转发给 `OpDefFactoryImpl` 单例 |
| [pkg_inc/base/asc/opdef/op_def_factory_impl.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/pkg_inc/base/asc/opdef/op_def_factory_impl.h) | Impl 类声明：真正持有 `std::map` 存储成员 |
| [base/asc/opdef/op_def_factory_impl.cc](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/asc/opdef/op_def_factory_impl.cc) | Impl 实现：emplace 登记、find 查询、Meyers 单例 |
| [inc/external/asc/register/op_config_registry.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/asc/register/op_config_registry.h) | SoC 级配置注册的对外接口与 `REGISTER_OP_AICORE_CONFIG` 宏 |
| [base/asc/opdef/op_config_registry.cc](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/asc/opdef/op_config_registry.cc) / [op_config_registry_impl.cc](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/asc/opdef/op_config_registry_impl.cc) | 配置注册的实现（同样是无锁单例 + map 存储） |
| [tests/ut/register/testcase/op_def_factory_unittest.cc](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/register/testcase/op_def_factory_unittest.cc) | 本讲实践的主战场：`OP_ADD` 的真实用法与查询断言 |

注意延续 u1-l3 的目录心智模型：`inc/external/asc/register/` 是对外 ABI 契约，`base/asc/opdef/` 是实现，而 `pkg_inc/base/asc/opdef/op_def_factory_impl.h` 是随包发布、供实现侧包含的内部桥接头（它**不在** `inc/external`，所以普通使用方看不到存储结构）。

## 4. 核心概念与源码讲解

### 4.1 OpDefFactory 与 OpDefFactoryImpl：门面与单例

#### 4.1.1 概念说明

工厂要解决的问题是：算子原型散落在各个算子仓的成千上万个 `.cc` 文件里，框架侧（图编译、代码生成器）需要一个**按名字查找**的集中入口。设计上拆成两层：

- `ops::OpDefFactory`：**门面**（facade），纯静态方法、无数据成员，是对外头文件里唯一的工厂类。
- `ops::OpDefFactoryImpl`：**实现**，持有真正的 `std::map` 存储，以 Meyers 单例形式活在 `libmetadef.so` 内。

分层的好处与 u1-l3 的「声明→桥接→实现」一致：对外头文件不暴露任何 STL 容器成员（`std::map` 布局随标准库版本变化，跨 ABI 危险），存储结构藏在 so 内部。

#### 4.1.2 核心流程

```text
算子仓代码                libmetadef.so
──────────               ─────────────────────────────────
OP_ADD(MyOp)
  └ 静态对象构造
     └ OpDefFactory::OpDefRegister("MyOp", creator)      ← 门面（编译进调用方）
        └ OpDefFactoryImpl::GetInstance().OpDefRegister  ← 转发进 so
           g_opsdef_creator.emplace("MyOp", creator)     ← map 登记
           g_ops_list.emplace_back("MyOp")               ← 列表登记

查询方（友元）
  OpDefFactory::OpDefCreate("MyOp")
     └ Impl::OpDefCreate → map.find("MyOp") → creator("MyOp") → 返回新造的 OpDef
```

#### 4.1.3 源码精读

先看门面头文件的两个类型别名与类声明：

[inc/external/asc/register/op_def_factory.h:L20-L41](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/asc/register/op_def_factory.h#L20-L41) —— 定义两代创建函数类型（`OpDefCreator` 是 `std::function`，`OpDefFuncPtr` 是裸函数指针）；类中 **public 的只有两个注册接口**，`OpDefCreate`（查询）、`GetAllOp`（枚举全部算子）、`OpTilingSinkRegister/OpIsTilingSink` 全部是 private，靠一长串 `friend` 授权给生成器类与 `optiling::DeviceOpImplRegister`。

关键一行是 weak 声明：

[inc/external/asc/register/op_def_factory.h:L24-L25](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/asc/register/op_def_factory.h#L24-L25) —— `OpDefRegisterV2` 带 `__attribute__((weak))`。weak 符号意味着：链接产物里如果没有强定义，取它的地址会得到 `nullptr`；如果有别的强定义（例如某个生成器工具想拦截注册），会覆盖 metadef 里的 weak 版本。下面 4.2 节会看到 `OP_ADD` 宏正是靠"判空"来利用这一点。

门面实现只是逐行转发：

[base/asc/opdef/op_def_factory.cc:L21-L31](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/asc/opdef/op_def_factory.cc#L21-L31) —— `OpDefRegister` / `OpDefRegisterV2`（同样是 weak 定义）/ `OpDefCreate` 三个方法全部一行转发给 `OpDefFactoryImpl::GetInstance()` 对应方法。

真正的存储在 Impl 侧：

[pkg_inc/base/asc/opdef/op_def_factory_impl.h:L34-L39](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/pkg_inc/base/asc/opdef/op_def_factory_impl.h#L34-L39) —— 四个成员：两代 creator 的 `std::map`（key 是 `ge::AscendString`，u2-l2 讲过它有 `std::hash` 特化，可作 map key）、一个记录注册顺序的 `vector`、一个 tiling sink 集合。

单例与登记逻辑：

[base/asc/opdef/op_def_factory_impl.cc:L14-L29](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/asc/opdef/op_def_factory_impl.cc#L14-L29) —— `GetInstance()` 是标准 Meyers 单例（函数内 `static OpDefFactoryImpl instance;`，C++11 保证并发首调用的初始化安全）；两个 `OpDefRegister*` 把 creator `emplace` 进 map、把名字 `emplace_back` 进列表，**固定返回 0，无任何判重与锁**。注意 `std::map::emplace` 的语义：同名第二次注册不会覆盖第一个 creator，但 `g_ops_list` 会多出一个重复名字。

#### 4.1.4 代码实践

**实践目标**：亲眼确认「门面无私有存储、Impl 持有存储、单例无线程锁」这三点。

**操作步骤**：

1. 在仓库根目录执行 `grep -n "mutex\|lock" base/asc/opdef/op_def_factory_impl.cc base/asc/opdef/op_def_factory.cc`，确认输出为空（无锁）。
2. `grep -n "static OpDefFactoryImpl instance" base/asc/opdef/op_def_factory_impl.cc`，定位 Meyers 单例所在行（应为第 15 行附近）。
3. 对照 [op_def_factory.h:L22-L41](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/asc/register/op_def_factory.h#L22-L41) 数一数 public 方法与 private 方法的数量。

**需要观察的现象 / 预期结果**：门面类 public 接口只有 2 个注册函数；实现侧找不到任何互斥量。若第 1 步 grep 有输出，说明仓库已变化，请以实际代码为准（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `OpDefFactory` 的查询接口 `OpDefCreate` 要设成 private 并配一堆 friend？
**答案**：按名查询并枚举全部算子是代码生成器/框架内部的能力，不是普通使用方的需求；收窄可见性可以避免业务代码在任意时刻随意触发 OpDef 构造（构造有副作用且涉及静态环境的初始化时序），也把「谁在消费工厂」固化在编译期友元清单里，便于 ABI 与行为治理。

**练习 2**：`OpDefRegister` 用 `map::emplace` 而不是 `map[name] = creator`，同名注册两次行为有何差异？
**答案**：`emplace` 在 key 已存在时不做任何事（第一个 creator 胜出），`operator[]` 会覆盖（后者胜出）。但无论哪种，`g_ops_list.emplace_back` 都会追加，所以列表里会出现重复名字，`GetAllOp().size()` 会大于去重后的算子数。

### 4.2 OP_ADD 宏：同一行代码的三种形态

#### 4.2.1 概念说明

算子作者实际只写一行 `OP_ADD(MyOp);`，但 [op_def_registry.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/asc/register/op_def_registry.h) 用 `#if defined(OP_PROTO_LIB) / #elif defined(OP_TILING_LIB) / #else` 让这一行在不同编译宏下产出**完全不同的注册产物**。这是理解"算子原型如何被发现"的钥匙：同一份算子源码，编进不同的 so，登记的东西不同。

#### 4.2.2 核心流程

三种形态展开后的产物：

| 编译宏 | 展开产物 | 登记到哪里 |
|---|---|---|
| `OP_PROTO_LIB`（原型 so） | 构造 `MyOp op("MyOp")` 实例取函数指针，链式挂到 `gert::OpImplRegisterV2`（InferShape/InferShapeRange/InferDataType） | OpImplRegistry（u4-l4 主题），**不进 OpDefFactory** |
| `OP_TILING_LIB`（tiling so） | 同上，但挂 Tiling/TilingParse，并调 5 次 `optiling::OpCheckFuncHelper` 注册 check 函数 | OpImplRegistry + opcheck（u4-l6 主题），**不进 OpDefFactory** |
| 都未定义（生成器/工具场景） | 静态 int 初始化时调用 `OpDefFactory::OpDefRegisterV2/Register` 登记创建函数 | **OpDefFactory** |

#### 4.2.3 源码精读

默认形态（唯一会进工厂的分支）：

[inc/external/asc/register/op_def_registry.h:L51-L58](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/asc/register/op_def_registry.h#L51-L58) —— `static int g_##opType##_added = [](){...}()`：立即调用的 lambda 在静态对象构造期执行。函数体内先判 `OpDefFactory::OpDefRegisterV2 != nullptr`——**取 weak 函数的地址做判空**：链接了 metadef 实现时走 V2（裸函数指针版本），否则退回 V1（`std::function` 版本）。注册的"创建函数"是无捕获 lambda `[](const char *name) -> ops::OpDef { return opType(name); }`，即"用算子名构造一个 `MyOp` 派生类实例"。这依赖一个事实：算子作者定义的 `MyOp` 继承自 `ops::OpDef`（单测里 `class AddAscendC : public OpDef`）。

原型库形态：

[inc/external/asc/register/op_def_registry.h:L17-L26](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/asc/register/op_def_registry.h#L17-L26) —— 先 `opType op(#opType);` 现场构造一次 OpDef 派生类（派生类构造函数里写好的 `GetInferShape()` 等成员随实例就绪），再把函数指针挂到 `gert::OpImplRegisterV2` 上。注意它**没有**调用 `OpDefFactory::OpDefRegister`——结合门面类里 `OpProtoGenerator` 等友元可以推断（此段为基于源码的推断，待确认）：生成器编译算子源码时**不定义**这两个宏，走 `#else` 分支把创建函数登记进工厂，再经友元身份调 `OpDefCreate`/`GetAllOp` 逐个枚举、序列化出算子原型描述文件；而正式发布的 so 则定义宏、只注册运行期真正需要的函数集。

tiling 库形态：

[inc/external/asc/register/op_def_registry.h:L28-L47](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/asc/register/op_def_registry.h#L28-L47) —— 除挂 Tiling/TilingParse 外，还用 `optiling::OpCheckFuncHelper` 注册 `FUNC_CHECK_SUPPORTED`、`FUNC_OP_SELECT_FORMAT` 等 5 类校验函数（预告 u4-l6）。

#### 4.2.4 代码实践

**实践目标**：用宏展开工具亲眼看到 `OP_ADD` 的产物，验证「静态初始化」这一时机。

**操作步骤**：

1. 在仓库根目录准备一个最小 cpp（示例代码，非项目文件）：

   ```cpp
   // /tmp/opadd_expand.cpp —— 仅用于观察宏展开
   #include "register/op_def_registry.h"
   class MyOp : public ops::OpDef {
    public:
     MyOp(const char *name) : OpDef(name) {}
   };
   OP_ADD(MyOp);
   ```

2. 找到 CANN 安装目录（`$ASCEND_HOME_PATH`）中 `include` 下的 `register/op_def_registry.h` 路径，执行：
   `g++ -std=c++11 -E -I$ASCEND_HOME_PATH/include /tmp/opadd_expand.cpp | grep -A 8 "g_MyOp_added"`。
3. 观察预处理输出中 `static int g_MyOp_added = []() { ... }()` 的完整内容。

**需要观察的现象 / 预期结果**：输出应包含对 `ops::OpDefFactory::OpDefRegisterV2` 的判空与调用，以及返回 `opType(name)` 的 lambda。若本机无 CANN 环境/头文件，可改为直接通读 [op_def_registry.h:L51-L58](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/asc/register/op_def_registry.h#L51-L58) 人工展开（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：`OP_ADD(MyOp)` 在 `OP_PROTO_LIB` 与默认两种编译形态下的最大差异是什么？
**答案**：默认形态把「创建函数」登记进 `OpDefFactory`（能按名现造 OpDef）；`OP_PROTO_LIB` 形态不进工厂，而是现场构造一次 OpDef 派生实例、把它携带的 InferShape/InferShapeRange/InferDataType 函数指针登记进 `gert::OpImplRegisterV2`。

**练习 2**：为什么默认分支要 `OpDefRegisterV2 != nullptr` 判空而不是直接调用 V2？
**答案**：`OpDefRegisterV2` 是 weak 符号。当编译单元没有链接到提供强/weak 定义的库时，其地址解析为 `nullptr`，直接调用会崩溃；判空后可退回始终有定义的 V1 注册路径，提高了宏在异构链接环境下的健壮性。

### 4.3 查询链路：OpDefCreate 的完整调用追踪

#### 4.3.1 概念说明

「按 opType 查询」不是查缓存的对象，而是**查创建函数并现场执行**。工厂存的是"配方"（creator），每次 `OpDefCreate` 都返回一个新构造的 `OpDef` 派生类实例（pimpl 壳，u4-l2）。这个设计让 OpDef 的构造逻辑始终由算子仓自己的代码决定，metadef 只维护名字→配法的映射。

#### 4.3.2 核心流程

```text
OpDefCreate("MyOp")
  ├─ g_opsdef_creator_v2.find("MyOp")   ─ 命中 → ptr("MyOp") → 返回     （V2 优先）
  ├─ 未命中 → g_opsdef_creator.find("MyOp") ─ 命中 → creator("MyOp") → 返回
  └─ 都未命中 → 返回 OpDef("default")   ← 哨兵值：名字为 "default" 的空壳
```

#### 4.3.3 源码精读

[base/asc/opdef/op_def_factory_impl.cc:L31-L45](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/asc/opdef/op_def_factory_impl.cc#L31-L45) —— `OpDefCreate` 先查 V2 map 再查 V1 map（两代注册体系并存时 V2 优先）；彻底找不到时返回 `OpDef("default")` 而非空对象——调用方需要靠 `GetOpType()` 是否等于 `"default"` 来识别失败，这是"哨兵值"式失败语义（u3-l4 见过同类设计）。`GetAllOp` 直接返回 `g_ops_list` 的**非常量引用**，调用方甚至可修改它——说明这个接口被假定只在受控的生成器/初始化代码里使用。

tiling sink 的登记与查询在同一文件：

[base/asc/opdef/op_def_factory_impl.cc:L47-L53](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/asc/opdef/op_def_factory_impl.cc#L47-L53) —— `OpTilingSinkRegister` 把算子名塞进 `std::set`，`OpIsTilingSink` 查 set 成员，用于标记"该算子的 tiling 下沉（在 host 侧执行）"这类元信息。

配套单测是整条链路的可执行版：

[tests/ut/register/testcase/op_def_factory_unittest.cc:L27-L32](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/register/testcase/op_def_factory_unittest.cc#L27-L32) —— 定义 `AddAscendC : public OpDef` 后一行 `OP_ADD(AddAscendC, None);` 完成注册（第二个参数被宏的 `...` 吞掉，无实际作用）。

[tests/ut/register/testcase/op_def_factory_unittest.cc:L73-L83](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/register/testcase/op_def_factory_unittest.cc#L73-L83) —— 断言 `GetAllOp()` 恰好含 3 个按**注册顺序**排列的名字（测试文件里静态对象的书写顺序），再 `OpDefCreate` 取回并断言 `GetOpType()` 一致。这验证了「vector 保序、map 可查」的双存储设计。

#### 4.3.4 代码实践

**实践目标**：写出「算子注册 → 工厂存储 → 按 opType 查询」的时序说明，并回答线程安全策略（本讲的核心实践任务）。

**操作步骤**：

1. 通读 [op_def_factory.cc](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/asc/opdef/op_def_factory.cc)（44 行）与 [op_def_factory_impl.cc](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/asc/opdef/op_def_factory_impl.cc)（54 行），按下表逐格填写：

   | 阶段 | 入口函数 | 关键语句 | 返回/失败语义 |
   |---|---|---|---|
   | 注册 | `OpDefFactory::OpDefRegister[V2]`（静态初始化期） | `g_opsdef_creator[_v2].emplace` + `g_ops_list.emplace_back` | 恒返回 0，不判重 |
   | 存储 | `OpDefFactoryImpl::GetInstance` | 函数级 `static instance` | C++11 保证初始化线程安全 |
   | 查询 | `OpDefFactory::OpDefCreate(name)` | V2 find → V1 find → 兜底 | 未命中返回 `OpDef("default")` |

2. 运行验证（u1-l2 建立的入口）：`bash tests/run_test.sh -u`，构建完成后用 `ctest -L ut -R op_def_factory`（在 `build_gcov/` 构建目录内执行）过滤只跑工厂相关用例。

**需要观察的现象 / 预期结果**：`OpDefFactoryUT` 下的 3 个测试全部 PASS；若环境无法编译（缺 CANN 依赖），第 1 步的静态分析仍可完成，运行部分标注「待本地验证」。

3. **线程安全结论**（对照源码回答）：
   - 工厂**没有任何互斥量**——注册（写 map）与查询（读 map）均不加锁；
   - 安全性完全依赖**时序约定**：所有注册都发生在 `.so` 被 `dlopen` 时的静态对象构造期，而动态加载器执行静态初始化是串行的；查询只发生在所有相关 so 加载完成之后；
   - 单例本身的并发首构造由 C++11 magic statics 兜底；
   - 推论：若有人在运行期多线程并发调用 `OpDefRegister` 或「边注册边查询」，`std::map` 的并发读写是未定义行为——这个风险由架构约定（注册仅在加载期）规避，而非由代码规避。

#### 4.3.5 小练习与答案

**练习 1**：`OpDefCreate` 查不到时返回 `OpDef("default")`，这种设计的隐患是什么？
**答案**：失败不报错、不返回空，调用方若忘记比对 `GetOpType() == "default"` 就会把空壳当正常原型用，错误被推迟到更远的地方才暴露；相比返回 `optional`/空指针 + 明确错误码，哨兵值方案省了判空但把责任转嫁给调用方。

**练习 2**：`GetAllOp()` 返回 `std::vector` 的非常量引用，为什么说这暴露了"受信任调用方"的假设？
**答案**：外部可拿到引用后增删元素，直接破坏工厂内部状态（例如清空列表导致后续 `OpDefCreate` 仍可用但 `GetAllOp` 失真）。敢这么设计是因为该接口是 private + friend，只有生成器等框架内部代码可达。

### 4.4 OpConfigRegistry：SoC 级配置的旁路注册

#### 4.4.1 概念说明

u4-l2 讲过 `OpDef::AICore().AddConfig(socVersion, config)` 是在算子定义内部声明 SoC 配置；`OpConfigRegistry` 提供**另一条路**：不碰算子定义代码，用独立宏 `REGISTER_OP_AICORE_CONFIG(opType, socVersion, func)` 把「某算子在某 SoC 上的配置生成函数」登记到一个独立单例。两条路的数据最终汇合——单测注释明确断言：OpDef 内 `AddConfig` 写入的配置**覆盖**宏注册的同名配置（"非空覆盖"合并规则的又一证据）。

#### 4.4.2 核心流程

```text
REGISTER_OP_AICORE_CONFIG(MyOp, ascend910b, func)
  └ 静态 uint32_t 初始化 → OpConfigRegistry::RegisterOpAICoreConfig
      └ OpConfigRegistryImpl 单例：funcData_[MyOp][ascend910b] = func

查询：GetOpAllAICoreConfig(MyOp) → map{ socVersion → func }（拷贝返回）
```

#### 4.4.3 源码精读

宏定义走 `__COUNTER__` 三层展开保证同文件多次使用不重名（与 u4-l1 的 `REGISTER_CUSTOM_OP` 同款技巧）：

[inc/external/asc/register/op_config_registry.h:L28-L39](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/asc/register/op_config_registry.h#L28-L39) —— 静态 `uint32_t` 变量的初始化 lambda 里构造一个 `OpConfigRegistry` 栈对象并调注册方法，仍是"静态对象构造期注册"。

门面与转发：

[base/asc/opdef/op_config_registry.cc:L15-L24](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/asc/opdef/op_config_registry.cc#L15-L24) —— `RegisterOpAICoreConfig` 一行转发 `OpConfigRegistryImpl::GetInstance().AddAICoreConfig`；自由函数 `GetOpAllAICoreConfig` 同样转发。

存储与判空：

[base/asc/opdef/op_config_registry_impl.cc:L16-L46](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/asc/opdef/op_config_registry_impl.cc#L16-L46) —— Meyers 单例；`AddAICoreConfig` 对 `name`/`socVersion` 判空后写入双层 map（算子名 → SoC 版本 → 函数指针）。注意这里的失败语义与工厂不同：参数非法时打 `GELOGE` 日志并直接 return（静默丢弃），不返回错误码；源码中判空代码重复出现了两轮（L22-L31 与 L34-L42），属于冗余防御，阅读时可视为一次判空。

覆盖关系的单测证据：

[tests/ut/register/testcase/op_def_factory_unittest.cc:L115-L135](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/register/testcase/op_def_factory_unittest.cc#L115-L35) —— `AddCustomAddConfigWithRegMacroTest` 同时用构造函数内 `AddConfig` 和 `REGISTER_OP_AICORE_CONFIG` 注册同一 SoC `ascend111y` 的配置，断言最终 `OpDefCreate` 出的 OpDef 里生效的是 `AddConfig` 写入的全 false 配置（见 L128 注释 "should overwrite"）。

#### 4.4.4 代码实践

**实践目标**：验证「宏注册的配置能被 `GetOpAllAICoreConfig` 查到，且会被 OpDef 内的 `AddConfig` 覆盖」。

**操作步骤**：

1. 通读 [op_def_factory_unittest.cc:L34-L71](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/register/testcase/op_def_factory_unittest.cc#L34-L71)：`AddCustomRegMacro`（只有宏注册）与 `AddCustomAddConfigWithRegMacro`（宏 + `AddConfig` 双注册）两个测试算子。
2. `bash tests/run_test.sh -u` 后在构建目录执行 `ctest -L ut -R op_def_factory -V` 查看详细输出。

**需要观察的现象 / 预期结果**：三个用例全部 PASS；`RegisterOpAICoreConfigTest` 证明宏注册路径可查（`regConfigs.size() == 1`、key 为 `ascendxxxy`），`AddConfigWithRegMacroTest` 证明覆盖方向是 `AddConfig` > 宏注册。无本地环境时标注「待本地验证」，静态阅读断言即可完成结论。

#### 4.4.5 小练习与答案

**练习 1**：`OpConfigRegistryImpl` 与 `OpDefFactoryImpl` 的线程安全策略相同吗？
**答案**：相同——都是 Meyers 单例 + STL map、全程无锁，安全性同样依赖"注册只发生在静态初始化期"这一时序约定。

**练习 2**：为什么 SoC 配置要单独搞一个注册宏，而不全部写进 OpDef 构造函数？
**答案**：解耦发布节奏与代码归属——SoC 适配（尤其是新硬件适配）可以只在配置侧追加宏注册，不必改动算子原型定义代码；两条注册路径经"非空覆盖"规则合并，兼顾了集中定义与旁路扩展两种诉求。

## 5. 综合实践

把本讲三块知识串成一个「注册到查询」的完整闭环（示例代码，非项目文件）：

1. **定义并注册**：仿照单测写一个 `MyAdd : public ops::OpDef`（构造函数里 `Input("x").Input("y").Output("z")`，沿用 u4-l2 的链式 API），文件末尾一行 `OP_ADD(MyAdd);`。
2. **查询验证**：写一个小测试：断言 `OpDefFactory::GetAllOp()`（可通过把测试写成工厂的友元、或直接复用单测中已有的测试夹具方式）包含 `"MyAdd"`；`OpDefCreate("MyAdd")` 返回的对象 `GetOpType() == "MyAdd"` 且能枚举出 2 个输入、1 个输出。
3. **配置旁路**：再为 `MyAdd` 追加 `REGISTER_OP_AICORE_CONFIG(MyAdd, ascend910b, [](){ return ops::OpAICoreConfig("ascend910b"); });`，断言 `GetOpAllAICoreConfig("MyAdd")` 能查到该 SoC。
4. **构建验证**：把测试文件放进 `tests/ut/register/testcase/`（u1-l2 讲过 `ut_register` 目标按目录组织、glob 收集源文件），`bash tests/run_test.sh -u` 后用 `ctest -R` 跑自己的用例。
5. **写时序说明**：在测试文件头部注释里，用 4.3.4 的三行表格格式写下「静态初始化注册 → 单例 map 存储 → OpDefCreate 查询」时序与线程安全结论。

预期：测试 PASS，且你能不看讲义复述出 weak 符号判空、`OpDef("default")` 哨兵、无锁单例三个设计点。整个实践在无昇腾硬件的环境下也可完成（stub 机制保证了 UT 不依赖真实设备）。

## 6. 本讲小结

- `OP_ADD` 宏靠「静态对象构造期注册」在 so 加载时把算子的**创建函数**登记进 `OpDefFactory`；同一行代码在 `OP_PROTO_LIB` / `OP_TILING_LIB` / 默认三种编译形态下分别产出 impl 函数注册、tiling+check 注册、工厂登记。
- 工厂是两层结构：`OpDefFactory` 门面（查询接口 private + friend）+ `OpDefFactoryImpl` Meyers 单例（`map<AscendString, creator>` 双代并存 + 保序 `vector` + tiling sink `set`）。
- `OpDefRegisterV2` 是 weak 符号，`OP_ADD` 默认分支靠判空选择 V2（裸函数指针）或 V1（`std::function`）注册路径。
- 查询 = 查配方并现场构造：V2 优先、V1 兜底、都未命中返回 `OpDef("default")` 哨兵空壳。
- 线程安全全程无锁，安全性来自「C++11 magic statics + 注册仅发生在 dlopen 静态初始化期」的时序约定，运行期并发写 map 是未定义行为。
- `OpConfigRegistry` 用同一套静态注册模式登记 SoC 级配置，与 OpDef 内 `AddConfig` 按"非空覆盖"合并，后者优先。

## 7. 下一步学习建议

本讲只解决了「算子原型（长什么样）」的注册与查询。下一讲 **u4-l4：OpImplRegistry 与 OpImplSpaceRegistry** 将进入 `OP_PROTO_LIB` 分支里已经露面的 `gert::OpImplRegisterV2`，讲解算子**实现函数集**（InferShape、Tiling 等）如何按 space 组织与注册——那是 `OP_ADD` 在真实发布 so 中的另一半产物。阅读建议：先重读 [op_def_registry.h:L17-L47](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/asc/register/op_def_registry.h#L17-L47) 带着问题去看 `OpImplRegisterV2` 的构造与析构做了什么；u4-l6 则会补完 `optiling::OpCheckFuncHelper` 那五个 check 注册的归宿。
