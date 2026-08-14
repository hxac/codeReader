# opcheck：算子正确性校验注册

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 opcheck 机制在算子交付流程中的定位——它注册的不是算子的执行逻辑，而是「针对一个具体算子实例，回答它是否被支持、该选什么格式」的查询函数。
2. 掌握 `optiling::OpCheckFuncHelper` 与 `OpCheckFuncRegistry` 的注册方式，理解「构造函数即注册」这一静态注册惯用法（与 u4-l3 的 OP_ADD、u4-l4 的 IMPL_OP 同源）。
3. 对比 opcheck 与 op_impl_registry 两套注册体系在函数签名、上下文载体、存储结构上的异同，能判断一个新需求该走哪条链路。

本讲依赖 u4-l3（静态对象构造期注册、Meyers 单例）与 u4-l2（OpDef/OpAICoreDef 链式 API）建立的概念。

## 2. 前置知识

**什么是「算子校验」？** 图编译时，框架拿到的是一个个具体的算子实例（带真实 shape、dtype、format、属性值）。在真正为它选择 kernel 之前，框架需要问算子仓几个问题：

- 这个算子实例在当前芯片上**支持不支持**？（check_supported）
- 如果输入 format 不明确，**推荐哪种格式**？（op_select_format）
- 这个算子的**支持信息**（如支持的分档、精度）是什么？（get_op_support_info）
- 有没有**专属的补充信息**要给下游工具？（get_op_specific_info）

回答这些问题的函数由算子作者实现，通过本讲的 opcheck 机制注册。它们与 tiling、InferShape 一样是算子的「实现函数」，但定位是**查询/校验**而非计算。

**函数指针签名**：所有 opcheck 函数都是 C 风格裸函数指针（不是 std::function），入参是 `const ge::Operator &`（图编译期的算子实例句柄）加一个 `ge::AscendString &` 出参，返回 `ge::graphStatus`。回忆 u2-l2：`AscendString` 是跨 ABI 的字符串封装，这里的出参 `result` 就是校验函数写给框架的「答案」。

**静态对象构造期注册**：在 so 被 dlopen 的瞬间，全局静态对象会先于任何显式调用完成构造。把「注册」这个动作放进一个全局对象的构造函数里，就能让注册自动发生——这是本仓 OpDefFactory、OpImplRegistry、opcheck 三套体系共同的底层技巧，u4-l3 已详细讲过。

**两级 map 检索**：opcheck 的核心数据结构是 `std::map<AscendString, std::map<AscendString, 函数指针>>`——外层 key 是 check_type（校验类型），内层 key 是 op_type（算子名）。这是它和 op_impl_registry（单层 op_type → 函数集结构体）在存储上的本质区别。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [inc/external/asc/register/op_check_register.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/asc/register/op_check_register.h) | 对外头文件：四类函数指针签名、`OpCheckFuncRegistry` 门面、`ReplayFuncHelper` |
| [inc/external/asc/register/op_def.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/asc/register/op_def.h) | 定义 `OP_CHECK_FUNC`/`PARAM_GENERALIZE_FUNC` 签名、`OpCheckFuncHelper` 声明，以及 `OpAICoreDef` 的 Set/Get 接口 |
| [base/asc/opcheck/op_check.cc](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/asc/opcheck/op_check.cc) | 门面与 Helper 的实现：全部一行转发到 Impl 单例；Helper 构造函数完成注册 |
| [pkg_inc/base/asc/opcheck/op_check_register_impl.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/pkg_inc/base/asc/opcheck/op_check_register_impl.h) | `OpCheckFuncRegistryImpl` 单例类的声明（内部桥接头文件） |
| [base/asc/opcheck/op_check_register_impl.cc](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/asc/opcheck/op_check_register_impl.cc) | 单例与四个 map 的注册/查询实现，编入 opp_registry 相关产物 |
| [inc/external/asc/register/op_def_registry.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/asc/register/op_def_registry.h) | `OP_ADD` 宏：OP_TILING_LIB 分支中把 OpAICoreDef 里的校验函数经 Helper 注册 |
| [base/asc/opdef/op_def_aicore.cc](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/asc/opdef/op_def_aicore.cc) | `OpAICoreDef` 的 SetCheckSupport/GetCheckSupport 等存取实现 |
| [tests/ut/register/testcase/op_check_unittest.cc](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/register/testcase/op_check_unittest.cc) | 本模块唯一的单元测试 |

## 4. 核心概念与源码讲解

### 4.1 opcheck 的函数签名体系与 check_type

#### 4.1.1 概念说明

opcheck 体系注册四类函数，每类的「问题域」不同：

| 函数指针类型 | 签名要点 | 回答的问题 |
|---|---|---|
| `OP_CHECK_FUNC` | `(const ge::Operator &, AscendString &)` → graphStatus | 算子实例是否支持/选什么格式 |
| `PARAM_GENERALIZE_FUNC` | 额外带 `generalize_config` 入参 | 如何把具体算子参数泛化（用于算子分档归一） |
| `GEN_SIMPLIFIEDKEY_FUNC` | `(const ge::Operator &, AscendString &)` → bool | 如何生成算子的简化 key |
| `REPLAY_FUNC` | `(ReplayFuncParam &, int32_t core_type)` → int32_t | 离线 replay（tiling 复算）时的参数回填 |

其中 `OP_CHECK_FUNC` 又按 check_type 细分为四种，check_type 是字符串常量。

#### 4.1.2 核心流程

```
算子作者侧                              框架查询侧
──────────                              ──────────
SetCheckSupport(func)                 GetOpCapability(check_type, op_type)
        │                                     │
        ▼                                     ▼
OP_ADD 宏展开时取出函数             两级 map 查找：
optiling::OpCheckFuncHelper(          map[check_type][op_type] -> func
  FUNC_CHECK_SUPPORTED,               查不到返回 nullptr + GELOGW
  "MyOp", func)
        │
        ▼
构造函数内调用 RegisterOpCapability
写入单例的 map
```

#### 4.1.3 源码精读

check_type 的四个字符串常量与两个核心签名定义在 op_def.h 的 `optiling` 命名空间里：

- [inc/external/asc/register/op_def.h:L32-L40](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/asc/register/op_def.h#L32-L40) — 定义 `FUNC_CHECK_SUPPORTED`（"check_supported"）、`FUNC_OP_SELECT_FORMAT`、`FUNC_GET_OP_SUPPORT_INFO`、`FUNC_GET_SPECIFIC_INFO` 四个 check_type 宏，以及 `OP_CHECK_FUNC`（`ge::graphStatus (*)(const ge::Operator &op, ge::AscendString &result)`）与 `PARAM_GENERALIZE_FUNC` 签名。注意这一段在 op_def.h 而不是 op_check_register.h，造成一条隐蔽的包含依赖：op_check_register.h 必须 include op_def.h 才能拿到 `OP_CHECK_FUNC`。

- [inc/external/asc/register/op_check_register.h:L29-L42](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/asc/register/op_check_register.h#L29-L42) — 定义 `ReplayFuncParam`（replay 场景的参数包：block_dim、tiling_data、kernel_name 等裸指针字段，纯 C 风格布局）与 `REPLAY_FUNC`、`GEN_SIMPLIFIEDKEY_FUNC` 两个签名。

`OpAICoreDef`（OpDef 的 AICore 配置段，见 u4-l2）提供校验函数的存取接口：

- [inc/external/asc/register/op_def.h:L423-L434](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/asc/register/op_def.h#L423-L434) — `SetCheckSupport`/`SetOpSelectFormat`/`SetOpSupportInfo`/`SetOpSpecInfo`（均收 `OP_CHECK_FUNC`）与 `SetParamGeneralize`，以及对应的五个 Get 方法。这就是算子作者在 OpDef 链式语法中挂校验函数的入口。

- [base/asc/opdef/op_def_aicore.cc:L157-L171](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/asc/opdef/op_def_aicore.cc#L157-L171) — 五个 Get 方法的实现：直接返回 `impl_->op_chk_support` 等成员引用。真实字段存放在 `pkg_inc/base/asc/opdef/op_def_impl.h` 的 `OpAICoreDefImpl` 中（见 [pkg_inc/base/asc/opdef/op_def_impl.h:L198-L202](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/pkg_inc/base/asc/opdef/op_def_impl.h#L198-L202)），默认值全为 `nullptr`——没写校验函数的算子，这些槽位就是空指针。

#### 4.1.4 代码实践

**实践目标**：确认「没注册校验函数的算子走 OP_ADD 时会发生什么」。

**操作步骤**：

1. 阅读 [inc/external/asc/register/op_def_registry.h:L35-L47](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/asc/register/op_def_registry.h#L35-L47) 的 OP_TILING_LIB 分支，注意第 40–44 行的五个 `OpCheckFuncHelper(...)` 调用是**无条件执行**的。
2. 对照 `OpAICoreDefImpl` 中五个字段的默认值 `nullptr`。
3. 回答：一个只写了 Tiling 函数的算子，`OpCheckFuncHelper(FUNC_CHECK_SUPPORTED, "MyOp", nullptr)` 会注册什么进 map？

**需要观察的现象 / 预期结果**：`RegisterOpCapability` 不判空，会把 `map["check_supported"]["MyOp"] = nullptr` 原样写入。查询侧 `GetOpCapability` 能在 map 里**找到**这个条目，但返回的函数指针是 `nullptr`——即「注册了空函数」和「从未注册」在查询结果上等价，都表现为「该算子无此校验能力」。待本地验证：可在单测里注册一个 nullptr 后调用 `GetOpCapability` 观察返回值。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `OP_CHECK_FUNC` 的第一个参数是 `const ge::Operator &` 而 tiling 函数的第一个参数是 `TilingContext *`？

**答案**：两类函数运行在不同的阶段。校验函数在**图编译期**被调用，此时算子还是图里的一个节点，宿主侧用 `ge::Operator` 句柄描述（可读 shape/format/属性）；tiling 函数在 exe_graph 执行链路上被调用，走的是 u3-l3 讲过的 POD 化 `TilingContext` 槽位体系。载体不同反映了框架两套体系（ge 编译体系 / gert 运行时体系）的分界。

**练习 2**：check_type 为什么用字符串而不是枚举？

**答案**：check_type 是注册与查询双方约定的 key。用字符串（`"check_supported"` 等宏）允许上层工具在不重新编译 metadef 的情况下扩展新的校验类型——新增一个 check_type 字符串就能往两级 map 里放新类别，不触碰枚举值这类 ABI 契约（对比 u2-l1 中 DataType 枚举「只能尾部追加」的约束）。

### 4.2 OpCheckFuncRegistry 门面与 OpCheckFuncRegistryImpl 单例

#### 4.2.1 概念说明

这一层的分工与 u4-l3 的 OpDefFactory/OpDefFactoryImpl 完全同构：

- **`OpCheckFuncRegistry`**（对外门面）：纯静态方法集合，声明在 inc/external，实现一行转发到 Impl，自己不持有状态。
- **`OpCheckFuncRegistryImpl`**（实现单例）：Meyers 单例（函数内 static 局部变量），真正持有四个 map，编入 metadef 的库里。

值得注意的一个细节：门面类在头文件里还声明了四个 static 成员变量（`check_op_capability_instance_` 等），但在本仓的 .cc 中**找不到它们的定义**——真实状态全部在 Impl 单例里。这四处声明是历史遗留，实际未被使用（也正因从未被 ODR-use 才不会引发链接错误）。读代码时不要被它们误导。

#### 4.2.2 核心流程

以 `check_supported` 的一次注册为例：

```
OpCheckFuncHelper(FUNC_CHECK_SUPPORTED, "MyOp", func)   ← 全局静态对象构造（so 加载时）
  └─ OpCheckFuncRegistry::RegisterOpCapability(check_type, op_type, func)   [op_check.cc:16]
       └─ OpCheckFuncRegistryImpl::GetInstance().RegisterOpCapability(...)  [op_check_register_impl.cc:21]
            └─ check_op_capability_instance_[check_type][op_type] = func    ← 两级 map 写入 + GELOGI
```

查询 `GetOpCapability` 是两次 `map::find`：先找 check_type，再找 op_type，任一 miss 即返回 `nullptr` 并打 GELOGW 告警。

#### 4.2.3 源码精读

- [base/asc/opcheck/op_check.cc:L16-L49](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/asc/opcheck/op_check.cc#L16-L49) — 门面类的 8 个静态方法（4 组注册/查询），每个都只有一行：取 `OpCheckFuncRegistryImpl::GetInstance()` 后转发。对外头文件的用户只需链接门面符号，不必感知 Impl。

- [base/asc/opcheck/op_check.cc:L51-L63](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/asc/opcheck/op_check.cc#L51-L63) — **本讲的核心**：`OpCheckFuncHelper` 的两个构造函数。三参数版本（check_type, op_type, OP_CHECK_FUNC）调用 `RegisterOpCapability`；两参数版本（op_type, PARAM_GENERALIZE_FUNC）调用 `RegisterParamGeneralize`。构造函数体内只有注册这一个动作——这就是「构造即注册」。`ReplayFuncHelper` 同理，构造时调用 `RegisterReplay`。

- [base/asc/opcheck/op_check_register_impl.cc:L16-L19](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/asc/opcheck/op_check_register_impl.cc#L16-L19) — Meyers 单例：`static OpCheckFuncRegistryImpl instance;`。C++11 起magic statics 保证线程安全的单次初始化。

- [base/asc/opcheck/op_check_register_impl.cc:L21-L43](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/asc/opcheck/op_check_register_impl.cc#L21-L43) — `RegisterOpCapability` 用 `check_op_capability_instance_[check_type][op_type] = func` 一行完成两级 map 写入（`operator[]` 不存在则自动创建），并打 GELOGI 记录注册计数；`GetOpCapability` 做两次 find，失败路径返回 `nullptr`。

- [base/asc/opcheck/op_check_register_impl.cc:L45-L96](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/asc/opcheck/op_check_register_impl.cc#L45-L96) — 其余三类函数的注册/查询，模式与上面完全一致：单层 map（key 是 op_type；replay 是 op_type → soc_version 两层）。所有失败语义统一为「返回 nullptr + 告警日志」，不抛异常、不返回错误码。

- [pkg_inc/base/asc/opcheck/op_check_register_impl.h:L25-L51](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/pkg_inc/base/asc/opcheck/op_check_register_impl.h#L25-L51) — Impl 类声明，私有区是四个 map 成员；构造/析构函数 `= default` 且私有，只留 `GetInstance()` 一个入口。注意这个头文件位于 `pkg_inc/base/` 而非 `inc/external/`，属于内部桥接头（呼应 u1-l3 的目录分层）。

**关于线程安全**：与 OpDefFactory 一样，这里全程无锁。注册依赖「只发生在静态初始化期（so dlopen 时）」的时序约定；查询若与后续注册并发则没有保护，但按约定注册完成后才进入查询阶段。

**关于消费方**：在本仓范围内检索，`GetOpCapability`/`GetGenSimplifiedKeyFun`/`GetReplay` 除了门面自身和单测外**没有任何调用者**——metadef 只提供注册表与查询接口，真正的消费方在上层组件（图编译/算子交付工具链，即调用 check_supported 等能力做支持性判定的部分），不在本仓库内（待确认具体仓与调用点，可从 CANN 安装目录中链接 libmetadef/opp_registry 的工具入手排查）。

#### 4.2.4 代码实践

**实践目标**：亲手验证「构造即注册 + 两级 map 查询」，并确认注册项在本仓内的消费边界。

**操作步骤**：

1. 阅读本模块唯一单测 [tests/ut/register/testcase/op_check_unittest.cc:L28-L40](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/register/testcase/op_check_unittest.cc#L28-L40)：它注册了一个 `GEN_SIMPLIFIEDKEY_FUNC`，然后验证「查不存在的名字得 nullptr、查注册过的名字得非空」。
2. 在仓库内检索消费方：
   ```bash
   grep -rn "GetOpCapability\|GetReplay(" --include="*.cc" --include="*.h" base/ inc/ tests/
   ```
3. 汇总检索结果：哪些是注册方（op_check.cc 门面、op_def_registry.h 宏）？哪些是查询方（单测）？除单测外还有没有产品代码在查询？

**需要观察的现象**：检索结果应只命中 op_check.cc / op_check_register_impl.cc（定义）、op_def_registry.h（间接注册入口）与 op_check_unittest.cc（测试查询）。

**预期结果**：确认「metadef 只造注册表，不消费注册表」——查询方在仓库之外的上层工具里。这一结论决定了：修改 opcheck 的**查询行为**影响的是上层工具；修改**注册链路**才影响算子仓。

**练习（思考）**：单测为什么测 `GEN_SIMPLIFIEDKEY_FUNC` 而不是最常用的 `OP_CHECK_FUNC`？——因为前者只需一个裸函数与单层 map，不涉及 `ge::Operator` 的构造，测试成本最低；这提示我们补测试时也应优先选依赖最少的切入点。

#### 4.2.5 小练习与答案

**练习 1**：`OpCheckFuncHelper` 的对象本身有状态吗？把 `optiling::OpCheckFuncHelper(FUNC_CHECK_SUPPORTED, "MyOp", func);` 写成一条**没有名字的临时对象语句**（如上，出现在 OP_ADD 宏的 lambda 里）能完成注册吗？

**答案**：能。Helper 没有任何成员变量，注册动作发生在构造函数体内；即使是立即析构的临时对象，构造已经把函数指针写进了单例 map，析构不做任何撤销。这正是它在宏里以临时对象语法使用的原因。

**练习 2**：同一算子重复注册同一个 check_type 会怎样？

**答案**：`map[check_type][op_type] = func` 是覆盖写，后注册者胜出，且没有告警（对比 u4-l4 OpImplSpaceRegistry 的「先到先得、不覆盖」语义）。由于注册发生在静态初始化期且每个算子通常只在一处注册，实际很少触发。

**练习 3**：门面类头文件里那四个未定义的 static map 成员，为什么不会导致链接错误？

**答案**：类的 static 成员只有被**使用**（ODR-use，如取地址或读到值）时才要求存在定义。本仓所有代码都走 Impl 单例，从未触碰门面的这四个成员，编译器不生成对它们的符号引用，链接期自然不报错。它们是残留的死声明。

### 4.3 注册入口：OP_ADD 宏如何把校验函数送进注册表

#### 4.3.1 概念说明

算子作者通常**不直接** new 一个 `OpCheckFuncHelper`，而是在 OpDef 定义里调用 `SetCheckSupport` 等链式方法，剩下的事由 `OP_ADD` 宏（u4-l3 讲过其三种分支）在 OP_TILING_LIB 编译模式下自动完成。

#### 4.3.2 核心流程

```
算子仓编写：                          OP_ADD(opType) 宏展开（OP_TILING_LIB 模式）：
class MyOp : public ops::OpDef {       static int g_MyOp_added = [](const char *name) {
  MyOp() {                                 MyOp op("MyOp");
    ...                                    gert::OpImplRegisterV2 impl("MyOp");
    AICore().SetCheckSupport(CheckSupport);  impl.Tiling(op.AICore().GetTiling());
  }                                        // ↓ 五个校验函数转存到 opcheck 注册表
                                           OpCheckFuncHelper(FUNC_CHECK_SUPPORTED, "MyOp",
                                                             op.AICore().GetCheckSupport());
                                           ...（其余四个同理）
                                           OpImplRegisterV2 implReg(impl);
                                           return 0;
                                         }("MyOp");
```

也就是说：`SetCheckSupport` 只是把函数指针暂存在 OpDef 的 Impl 字段里；OP_ADD 宏在静态初始化期把它**搬运**到 opcheck 注册表，同时把 Tiling 函数搬运到 op_impl_registry。一条 OP_ADD 同时喂两套注册体系。

#### 4.3.3 源码精读

- [inc/external/asc/register/op_def_registry.h:L28-L47](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/asc/register/op_def_registry.h#L28-L47) — OP_TILING_LIB 分支的完整宏体。第 40–44 行连续五个 `optiling::OpCheckFuncHelper(...)`：前四个用 check_type 宏 + `op.AICore().GetXxx()` 取出暂存的 `OP_CHECK_FUNC`，第五个注册 `PARAM_GENERALIZE_FUNC`。注意 lambda 立即调用、结果赋给 `static int g_##opType##_added`——静态变量保证 lambda（即全部注册动作）在 so 加载时执行一次。

- [base/asc/opdef/op_def_aicore.cc:L113-L131](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/asc/opdef/op_def_aicore.cc#L113-L131) — `SetCheckSupport` 等五个 Set 方法的实现，转发到 `impl_->SetCheckSupport(this, func)` 写入 OpAICoreDefImpl 字段。链式返回 `OpAICoreDef &`，与 u4-l2 讲过的 Builder 风格一致。

- 对比另外两个分支：[inc/external/asc/register/op_def_registry.h:L17-L27](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/asc/register/op_def_registry.h#L17-L27)（OP_PROTO_LIB，只注册推理函数集）和 [L49-L58](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/asc/register/op_def_registry.h#L49-L58)（默认，只注册 OpDef 创建函数）都**不**注册校验函数——opcheck 注册只发生在 tiling 库（OP_TILING_LIB）里，与「校验函数围绕 tiling/支持性判定展开」的定位相符。

#### 4.3.4 代码实践

**实践目标**：写出一个自定义 check 函数的完整注册示例（本讲规格要求的实践产出）。

**操作步骤**：

以下为**示例代码**（仿照算子仓的典型写法组织，非本仓已有文件）：

```cpp
// 假想算子 MyAdd 的 opcheck 注册示例（示例代码）
#include "register/op_def_registry.h"

// 1. 校验函数本体：签名必须是 OP_CHECK_FUNC
static ge::graphStatus MyAddCheckSupport(const ge::Operator &op, ge::AscendString &result) {
  // 读取算子实例的输入描述，判断是否支持（示意）
  auto desc = op.GetInputDesc(0);
  if (desc.GetDataType() == ge::DT_FLOAT) {
    result = ge::AscendString("support: float on aicore");  // 把答案写给框架
    return ge::GRAPH_SUCCESS;
  }
  return ge::GRAPH_FAILED;  // 不支持
}

// 2. OpDef 定义中暂存校验函数
class MyAdd : public ops::OpDef {
 public:
  MyAdd() : ops::OpDef("MyAdd") {
    this->Input("x").Input("y").Output("z");
    this->AICore().SetCheckSupport(MyAddCheckSupport);  // 链式挂接
  }
};

// 3. OP_ADD 触发注册（OP_TILING_LIB 编译模式下，
//    宏会自动执行 OpCheckFuncHelper(FUNC_CHECK_SUPPORTED, "MyAdd", MyAddCheckSupport)）
OP_ADD(MyAdd);
```

若不走 OpDef 而想直接注册（例如独立工具场景），也可以手动构造 Helper（示例代码）：

```cpp
// 直接注册：so 加载时构造临时对象即完成登记
static optiling::OpCheckFuncHelper g_my_checker(  // 命名静态变量，生命周期贯穿进程
    optiling::FUNC_CHECK_SUPPORTED, "MyAddDirect", MyAddCheckSupport);
```

**需要观察的现象**：以上为源码阅读型实践，编译运行需接入算子仓工程，待本地验证。可先做纯逻辑检查：对照 4.3.3 的宏体，逐步确认第 3 步的 `OP_ADD(MyAdd)` 展开后 `op.AICore().GetCheckSupport()` 取到的正是第 2 步 `SetCheckSupport` 暂存的 `MyAddCheckSupport`。

**预期结果**：so 被 dlopen 后，`OpCheckFuncRegistryImpl::GetInstance()` 的 `check_op_capability_instance_["check_supported"]["MyAdd"]` 处即有 `MyAddCheckSupport`；上层工具调用 `OpCheckFuncRegistry::GetOpCapability("check_supported", "MyAdd")` 可取回该指针（本仓内无消费方，需在上层工具中验证，待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：如果算子作者忘了调用 `SetCheckSupport`，OP_ADD 展开的那行 `OpCheckFuncHelper(FUNC_CHECK_SUPPORTED, "MyAdd", op.AICore().GetCheckSupport())` 会注册什么？

**答案**：注册一个值为 `nullptr` 的条目（见 4.1.4 的分析）。查询时 map 能命中但返回空指针，效果等同于「无此能力」，不会崩溃。

**练习 2**：为什么 `GEN_SIMPLIFIEDKEY_FUNC` 和 `REPLAY_FUNC` 没有进 OP_ADD 宏，只能手动注册？

**答案**：这两类函数不属于 OpDef 原型描述的一部分（OpAICoreDefImpl 没有对应字段），它们服务于离线分析/replay 等工具场景，由需要的组件在自己的 so 里用 `OpCheckFuncRegistry::RegisterXxx` 或 Helper 手动登记。OP_ADD 只搬运 OpDef 里「暂存过」的函数。

### 4.4 opcheck 与 op_impl_registry 两套注册的对比

#### 4.4.1 概念说明

一个算子的「行为函数」有两条注册通道，初学者容易混淆。对比如下：

| 维度 | opcheck（本讲） | op_impl_registry（u4-l4） |
|---|---|---|
| 注册什么 | 校验/查询类函数（check_supported、select_format、replay 等） | 执行链路函数（InferShape、Tiling、TilingParse 等约 20 个阶段函数） |
| 函数载体 | `const ge::Operator &` + `AscendString &`（ge 编译期句柄） | `gert::XxxContext *`（POD 运行时上下文） |
| 聚合方式 | 分散存放在 4 个 map，按 check_type/op_type 单函数检索 | 打包成 `OpImplFunctionsV2` 结构体，一个 op_type 一份 |
| 注册入口 | `OpCheckFuncHelper` 构造 / `RegisterXxx` 静态方法 | `IMPL_OP` 宏 → `OpImplRegisterV2` 链式 + 拷贝构造触发 |
| 跨 so 传递 | 直接写进 metadef 侧单例 map，同进程可见 | C API 两步协议（GetRegisteredOpNum → GetOpImplFunctionsV2）拷贝出注册表再合并 |
| 重复注册语义 | 覆盖（后到者胜，无告警） | 先到先得、不覆盖、重复告警 |
| 失败语义 | 返回 nullptr + GELOGW | 哨兵值 / GRAPH_FAILED |
| 本仓内消费方 | 无（上层工具消费） | OppSoManager 加载链路（u4-l5） |

#### 4.4.2 核心流程

两套体系在 OP_TILING_LIB 分支的 OP_ADD 宏里**同时**被喂养，但此后分道扬镳：

```
OP_ADD(opType)（OP_TILING_LIB）
  ├─ impl.Tiling(...)                        → op_impl_registry（进程内单例 + 跨 so 聚合）
  ├─ OpImplRegisterV2 implReg(impl);           拷贝构造触发注册（u4-l4）
  └─ OpCheckFuncHelper(...) × 5              → opcheck 注册表（仅本进程 map，无跨 so 协议）
```

#### 4.4.3 源码精读

- 新体系（op_impl_registry 侧）的同名能力对照：[docs/zh/api/gert_namespace/opimplregisterv2/CheckSupport.md](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/docs/zh/api/gert_namespace/opimplregisterv2/CheckSupport.md) 记录了 `OpImplRegisterV2::CheckSupport`，其函数类型为 `OP_CHECK_FUNC_V2 = ge::graphStatus (*)(const OpCheckContext *context, ge::AscendString &result)`——同样语义的 check_support，在 v2 体系里改用 `OpCheckContext` 上下文而非 `ge::Operator`。也就是说「check_support」能力在两套体系里各有一条注册链，opcheck（`ge::Operator` 版）是伴随 asc OpDef 体系的老通道。

- 两套通道在 OP_ADD 里的并置证据：[inc/external/asc/register/op_def_registry.h:L37-L45](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/asc/register/op_def_registry.h#L37-L45) — 同一个 lambda 里，第 37–39 行喂 op_impl_registry（`impl.Tiling(...)`、`impl.TilingParse<...>(...)`），第 40–44 行喂 opcheck（五个 Helper）。

#### 4.4.4 代码实践

**实践目标**：通过单测亲手跑一遍 opcheck 的注册-查询闭环。

**操作步骤**：

1. 阅读 [tests/ut/register/testcase/op_check_unittest.cc:L16-L18](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/register/testcase/op_check_unittest.cc#L16-L18)：测试用的裸函数 `testFunc`（注意它返回 bool 而 `GEN_SIMPLIFIEDKEY_FUNC` 期望 bool，与 `OP_CHECK_FUNC` 的 graphStatus 不同——这正是四类签名各自独立的体现）。
2. 仿照该文件在 `tests/ut/register/testcase/` 下新增一个用例（**示例代码**）：
   ```cpp
   ge::graphStatus myCheck(const ge::Operator &op, ge::AscendString &result) {
     result = ge::AscendString("ok");
     return ge::GRAPH_SUCCESS;
   }
   TEST_F(OpCheckAPIUT, CapabilityTest) {
     optiling::OpCheckFuncRegistry::RegisterOpCapability(
         ge::AscendString("check_supported"), ge::AscendString("MyAdd"), myCheck);
     EXPECT_EQ(optiling::OpCheckFuncRegistry::GetOpCapability(
         ge::AscendString("check_supported"), ge::AscendString("NotReg")), nullptr);
     EXPECT_EQ(optiling::OpCheckFuncRegistry::GetOpCapability(
         ge::AscendString("check_supported"), ge::AscendString("MyAdd")), myCheck);
   }
   ```
3. 运行：`bash tests/run_test.sh -u`。新文件位于 `tests/ut/register/` 下，[tests/ut/register/CMakeLists.txt:L22](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/register/CMakeLists.txt#L22) 用 `GLOB_RECURSE` 自动收集该目录全部 `.cc`，无需改 CMake（`CONFIGURE_DEPENDS` 保证新增文件被感知）。

**需要观察的现象**：`ut_register` 目标编译通过，gtest 报告中新增的 `CapabilityTest` 通过。

**预期结果**：两个断言均成立——查询未注册组合返回 `nullptr`，查询已注册组合返回注册时的函数指针。注意运行需要 u1-l2 讲过的本地构建环境（`ASCEND_HOME_PATH` 等），待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 opcheck 不需要 u4-l4 那套「C 导出两步协议」来跨 so 传递注册表？

**答案**：opcheck 的注册直接写入 metadef/opp_registry 库内的**进程级单例 map**。注册方（算子 so）与查询方（上层工具）链接的是同一个 metadef 库实例，dlopen 用 `RTLD_GLOBAL`（u4-l5）保证符号唯一，注册完成即全进程可见。而 op_impl_registry 的注册表位于**各个算子 so 内部**，框架必须经 dlsym 把它们拷贝出来聚合，所以才需要两步协议。

**练习 2**：如果要给算子新增一种「精度档位查询」check 函数，走 opcheck 还是 op_impl_registry 改动更小？

**答案**：走 opcheck——只需与消费方约定一个新的 check_type 字符串（如 `"get_precision_level"`），用 `OpCheckFuncHelper` 注册即可，两级 map 自动容纳新类别，metadef 侧零代码改动。走 op_impl_registry 则要给 `OpImplFunctionsV2` 增加函数指针字段，动到有 st_size/version 守护的 ABI 敏感结构体（u4-l4），代价高得多。

**练习 3**：两套体系的「重复注册语义」不同（覆盖 vs 先到先得），哪种更安全？

**答案**：先到先得更安全——它保证多个 so 注册同一算子时行为确定（第一个生效）并给出告警，便于发现交付件冲突；覆盖语义下后加载的 so 会静默改写先注册的函数，问题更隐蔽。opcheck 因为注册源单一（每个算子的 OP_ADD 只在自己 so 里执行一次），覆盖语义实际风险可控。

## 5. 综合实践

**任务：为一个假想算子补全 opcheck 链路并验证。**

1. **定义与暂存**：参照 4.3.4 的示例代码，写出 `MyAdd` 的 OpDef，包含 `SetCheckSupport` 与 `SetOpSelectFormat` 两个校验函数（后者示意：输入 format 为 ND 时推荐 `FORMAT_ND`，写入 result）。
2. **跟踪搬运**：对照 [inc/external/asc/register/op_def_registry.h:L28-L47](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/asc/register/op_def_registry.h#L28-L47)，在纸上写出 `OP_ADD(MyAdd)` 在 OP_TILING_LIB 模式下展开后的完整语句序列，标出哪几行进 op_impl_registry、哪几行进 opcheck。
3. **绕过宏直接注册并测试**：不走 OpDef，直接在 `tests/ut/register/testcase/` 新增测试文件，用 `OpCheckFuncRegistry::RegisterOpCapability` 注册你的两个函数，断言 `GetOpCapability("check_supported", "MyAdd")` 与 `GetOpCapability("op_select_format", "MyAdd")` 均返回注册指针、查询 `"get_op_support_info"`（未注册）返回 `nullptr`。
4. **构建验证**：`bash build.sh` 确认主库编译不受影响，`bash tests/run_test.sh -u` 跑 `ut_register`（测试目录 GLOB 自动收集新文件）。无本地环境时写出完整预期输出，标注「待本地验证」。
5. **边界分析**：用 grep 确认本仓内 `GetOpCapability` 无产品代码调用，写一段话说明「注册在本仓、消费在上层」对修改风险评估的意义。

## 6. 本讲小结

- opcheck 注册的是**校验/查询类**函数（check_supported、op_select_format、get_op_support_info、get_op_specific_info、param generalize、simplified key、replay），载体是 `ge::Operator` 编译期句柄，与 op_impl_registry 的执行链路函数互补。
- 分层与 u4-l3 同构：`OpCheckFuncRegistry` 门面（inc/external）一行转发到 `OpCheckFuncRegistryImpl` Meyers 单例（base/asc/opcheck），状态是按 check_type→op_type 的两级 map 及两个单层 map。
- `OpCheckFuncHelper` 构造函数即注册：so dlopen 时静态对象构造，把函数指针写进单例 map；注册不判空，未设置校验函数的算子会注册 nullptr 条目，查询效果等同「无此能力」。
- 常规入口是 OpDef 的 `SetCheckSupport` 等链式方法 + `OP_ADD` 宏（仅 OP_TILING_LIB 分支）自动搬运；也可以用 Helper/静态方法直接注册。
- 本仓内没有任何 `GetOpCapability` 等查询的产品代码调用者——metadef 只造注册表，消费方在上层工具链。
- 与 op_impl_registry 的关键差异：map vs 函数集结构体、直接写单例 vs 跨 so 两步协议、覆盖 vs 先到先得、返回 nullptr vs 哨兵值。

## 7. 下一步学习建议

本讲收尾单元四的注册链路。接下来建议：

1. 进入 u5-l1（ContextBuilder），回到运行时侧，看框架宿主如何组装 u3 系列讲过的各类上下文对象。
2. 若对「注册表如何被 so 加载驱动」意犹未尽，可回头重读 u4-l5 的 `OppSoManager`，把「dlopen → 静态构造 → OP_ADD 注册 → 两步取数」的完整时序串成一张图。
3. 想验证本讲消费方边界的读者，可在 CANN 安装目录中对链接 `libmetadef`/`libopp_registry` 的工具用 `strings | grep check_supported` 排查，确认哪些工具在查询 opcheck 注册表（外部环境，待确认）。
