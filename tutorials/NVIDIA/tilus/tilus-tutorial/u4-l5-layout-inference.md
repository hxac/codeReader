# 布局自动推理（Layout Inference）

## 1. 本讲目标

在前面四讲里，我们认识了四种张量与四种布局，也学了布局的代数运算（`compose`/`divide`/`reduce`）。但有一个关键问题始终悬而未决：**用户写内核时几乎从不手动指定布局**——`register_tensor`、`shared_tensor` 创建时 `optional_layout` 通常是 `None`，那这些布局到底是谁、在什么时候、按什么规则填上去的？

答案就是本讲的主题：**布局自动推理（Layout Inference）**。它是 Tilus 把「人类易写的高层 IR」翻译成「硬件能跑的低层 IR」的核心一公里，也是 Tilus 相对 Triton「黑箱布局托管」最大的差异化能力之一。

学完本讲，你应当能够：

1. 说清**推理规则（LayoutInferenceRule）**与**验证规则（LayoutValidationRule）**两种规则的接口、注册方式与查询机制；
2. 在一张给定的指令上，判断布局是**前向传播**（输入→输出）还是**反向传播**（输出→输入），并能画出传播方向；
3. 描述 `infer_layout` 的**优先级驱动的迭代求解**（不动点循环）过程，理解它如何从「种子」规则出发逐级填充缺失布局；
4. 理解验证规则如何用 `MultiFunction.cover` 检查布局一致性，把推理结果「把关」一遍。

---

## 2. 前置知识

本讲建立在 u3（Tilus IR）与 u4 前四讲（布局系统）之上。开始前，请确认你已理解以下几点：

- **指令三段式结构**（u3-l4）：每条 `Instruction` 由 `output`、`inputs`、`attributes` 组成；功能指令与副作用指令的区别由白名单判定。
- **四种张量与 `optional_layout`**（u4-l1）：`RegisterTensor`/`SharedTensor`/`TMemoryTensor` 持有可空的 `optional_layout` 字段，遵循「`has_layout()` / `.layout`（未绑定抛错）/ `with_layout()`（返回新对象）」的三态协议；`GlobalTensor` 的布局必填、不参与推理。
- **布局是纯函数**（u4-l2/u4-l3）：布局把「逻辑元素索引」映射到「物理位置」（线程号 / bank / 字节地址 / TMEM 坐标）。
- **布局代数**（u4-l4）：`compose`（外层×内层拼接 mode）、`divide`（剥除子布局）、`reduce_to`（把某维改成复制以缩小形状）、`spatial`/`local`/`replicated` 构造原语。
- **MultiFunction**（u4-l2/u4-l4）：把「逻辑索引→线程号集合」抽象成的可复合多值函数；`spatial_mfunction` 是布局的线程映射函数，`cover` 判断一个线程映射是否「覆盖」另一个。

一句话回顾：**布局推理要做的，就是把程序里所有 `optional_layout is None` 的寄存器/共享/TMEM 张量，依据指令语义补上一个相容的布局。**

> 术语提示：本讲中「推理（inference）」指「补全缺失布局」，「验证（validation）」指「检查已存在布局是否彼此相容」。两者是不同规则，分开注册。

---

## 3. 本讲源码地图

本讲涉及的关键文件，按「规则定义 → 规则编排 → 规则调用」的顺序：

| 文件 | 作用 |
| --- | --- |
| `python/tilus/ir/layout/inference/rule.py` | 定义两种规则基类、注册表与 `register_rule` 装饰器、按 MRO 查询规则的函数。 |
| `python/tilus/ir/layout/inference/order.py` | 把所有推理规则排成一个全局优先级表 `rule2order`。 |
| `python/tilus/ir/layout/inference/inference.py` | 推理主算法 `infer_layout` 与验证入口 `verify_layouts`，含不动点循环。 |
| `python/tilus/ir/layout/inference/inference_rules/*.py` | 每条指令的具体推理规则（如 `BinaryRule`、`MmaDotRule`、`LoadGlobalRule`）。 |
| `python/tilus/ir/layout/inference/validation_rules/*.py` | 每条指令的验证规则（如 `BinaryRule.validate` 用 `cover`）。 |
| `python/tilus/transforms/layout_inference.py` | 把推理封装成一个 `LayoutInferencePass`，并先应用用户手写的 `AnnotateLayoutInst` 注解。 |
| `python/tilus/transforms/__init__.py` | `get_default_passes()`，决定推理在整个变换流水线中的位置（出现两次）。 |

阅读建议：先看 `rule.py`（接口）→ 再看 `inference.py`（算法骨架）→ 最后挑 2-3 条具体规则（`elementwise_binary.py`、`mma_dot.py`、`ldst_global.py`）对照理解。

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. **4.1 规则体系**：推理规则、验证规则与注册表；
2. **4.2 前向与反向**：布局传播的方向；
3. **4.3 优先级与迭代求解**：`infer_layout` 主循环；
4. **4.4 验证与覆盖**：`cover` 如何把关一致性。

---

### 4.1 规则体系：推理规则、验证规则与注册表

#### 4.1.1 概念说明

布局推理是一个**约束求解**问题：每个张量的布局是一个未知量，每条指令对它输入/输出张量的布局关系施加一组约束。Tilus 没有写一个统一的求解器，而是采用「**每条指令自带规则**」的分散式设计：

- **推理规则（LayoutInferenceRule）**：给定一条指令，若它部分操作数已有布局，规则负责推导出其余操作数的布局。一条指令可以注册**多个**推理规则（按优先级依次尝试）。
- **验证规则（LayoutValidationRule）**：给定一条所有操作数都已有布局的指令，规则负责判定这些布局是否**彼此相容**。一条指令只能注册**一个**验证规则。

为什么要分开？因为「能推出一个可行解」和「给定的解合法」是两个不同的问题。推理负责生成，验证负责把关；推理在 `infer_layout` 内部循环里跑，验证在循环结束后由 Pass 统一跑一遍。

#### 4.1.2 核心流程

规则的「注册—查询」流程：

```
模块导入 inference_rules / validation_rules
        │  （装饰器在导入时执行）
        ▼
register_rule(InstCls) 装饰 RuleClass
        │
        ├── 是 LayoutValidationRule 子类？ → _validation_rules[InstCls] = RuleClass（唯一）
        └── 是 LayoutInferenceRule 子类？ → _inference_rules[InstCls].append(RuleClass)（可多个）
        │
查询时 get_inference_rules(inst)
        │
        ▼
若 inst 的类未直接注册 → 沿 __mro__ 找父类的规则（子类自动继承）
```

关键点：推理规则用 `defaultdict(list)`，可以叠多个；验证规则用普通 dict，重复注册会直接抛 `ValueError`。

#### 4.1.3 源码精读

两种规则基类都只定义了一个静态方法，接口非常薄。推理规则返回「张量→布局」的映射（空映射表示「我推不出来」）：

[`rule.py:56-88`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/inference/rule.py#L56-L88) —— `LayoutInferenceRule.inference` 接收一个 `LayoutInferenceContext` 与指令，返回 `dict[Tensor, Layout]`。注释明确：**「It may only infer the layouts for part of the tensors」**——一条规则可以只补全部分操作数，剩下的留给别的规则或下一轮。

验证规则更简单，只返回布尔：

[`rule.py:37-53`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/inference/rule.py#L37-L53) —— `LayoutValidationRule.validate` 只问一句「这条指令当前的布局组合合法吗」。

`LayoutInferenceContext` 携带推理所需的上下文信息：

[`rule.py:29-34`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/inference/rule.py#L29-L34) —— 包含 `num_threads` / `thread_begin` / `thread_end`（当前指令所处线程组的范围）与 `analysis`（标量分析结果，含整除性/上下界）。这两类信息后文会用到：MMA 规则用 `num_threads` 算 warp 数，全局加载规则用 `analysis` 决定向量化宽度。

注册表与装饰器：

[`rule.py:91-112`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/inference/rule.py#L91-L112) —— 两个全局 dict `_inference_rules`（`defaultdict(list)`）与 `_validation_rules`。`register_rule(inst_type)` 返回一个装饰器，依据 `issubclass` 自动分流到对应字典；验证规则重复注册会抛错，推理规则则 `append` 进列表。

查询时支持继承：

[`rule.py:115-131`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/inference/rule.py#L115-L131) —— `get_inference_rules` 若该指令类未直接注册，就沿 `__mro__`（方法解析顺序）找最近的有规则的父类。这意味着：新增一条 `AddInst` 的子类指令，会自动继承 `AddInst` 的规则，无需重复注册。

**「占位规则」模式**：并非所有指令都关心布局。像 `StoreGlobalInst`（结果已写回显存）、`AllocateRegisterInst`、各种异步搬运指令，它们的寄存器/共享张量布局由「别的指令」决定，自己不做任何推理或校验。Tilus 用两个「万能占位规则」统一处理：

[`inference_rules/empty_rule.py:51-58`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/inference/inference_rules/empty_rule.py#L51-L58) —— `EmptyRule.inference` 恒返回 `{}`（不推导任何布局），被 `StoreGlobalInst`、`AllocateRegisterInst`、`FreeSharedInst` 等十余条指令共用。

[`validation_rules/always_ok.py:100-103`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/inference/validation_rules/always_ok.py#L100-L103) —— `AlwaysOkayRule.validate` 恒返回 `True`（任何布局都放行），被 `AllocateSharedInst`、`LoadSharedInst`、`StoreGlobalInst` 等共用。

> 这两个占位规则解释了一个常见困惑：为什么 `StoreGlobalInst` 在推理列表里排在很后面、却从不主动定布局？因为它只是「读」一个已被定好布局的寄存器张量去写显存，布局的来源在别处（如 `dot` 或 `load_global`）。

#### 4.1.4 代码实践

**实践目标**：熟悉规则的注册位置与分流。

**操作步骤**（源码阅读型）：

1. 打开 `python/tilus/ir/layout/inference/inference_rules/elementwise_binary.py`，看 `@register_rule(AddInst)` 等 6 个装饰器叠在同一个 `BinaryRule` 上——这说明**多个指令类共用一条推理规则**。
2. 打开 `python/tilus/ir/layout/inference/validation_rules/elementwise_binary.py`，确认它也有一个同名的 `BinaryRule`，但这次继承的是 `LayoutValidationRule`，落在 `_validation_rules` 字典里。**两个 `BinaryRule` 是不同模块里的不同类，只是碰巧同名**。
3. 打开 `validation_rules/always_ok.py`，数一下 `AlwaysOkayRule` 头上有多少个 `@register_rule`，体会「占位规则」覆盖面之广。

**需要观察的现象**：推理规则可一对多（一条规则管多个指令）、也可多对一（一个指令挂多条规则，如 `LoadSharedInst` 挂了 4 条 `LoadSharedInfer*Rule`）；验证规则严格一对一。

**预期结果**：能口头说出「`AddInst` 的推理规则与验证规则分别注册在哪个字典、由哪个装饰器触发」。

#### 4.1.5 小练习与答案

**练习 1**：如果给同一个指令类用 `@register_rule` 注册了两个 `LayoutValidationRule`，会发生什么？

> **答案**：第二次注册时 `inst_type in _validation_rules` 为真，装饰器抛 `ValueError("Validation rule for ... is already registered")`（见 `rule.py:103-104`）。验证规则必须唯一。

**练习 2**：新增一个 `MyAddInst(AddInst)` 子类却不写任何规则，它的布局能被推理吗？

> **答案**：能。`get_inference_rules` 会沿 `__mro__` 找到父类 `AddInst` 注册的 `BinaryRule` 并返回它（见 `rule.py:124-128`）。这是「子类自动继承父类规则」的机制。

---

### 4.2 前向与反向：布局如何传播

#### 4.2.1 概念说明

一条指令的操作数里，有些张量已经有布局、有些还没有。规则要做的，就是「**从已知的布局，推出未知的布局**」。根据已知量在输入侧还是输出侧，传播分两个方向：

- **前向传播（forward）**：输入有布局 → 推输出布局。最典型的场景是「逐元素运算」——输出每个元素只依赖输入对应位置的元素，所以输出的排布应当与输入一致。
- **反向传播（backward）**：输出有布局 → 推输入布局。当输出被「下游」某个强约束指令（如 `store_global`、`dot` 的累加器）定型时，反向把约束传回输入。

还有一类特殊的**种子规则（seed）**：当一条指令的所有操作数都没有布局时，它能凭指令自身的 dtype/shape/参数**凭空生成**一组默认布局。这类规则是整个推理的「火种」——没有它们，前向/反向都无从启动。典型的种子规则是 `LoadGlobalRule`（全局加载定下寄存器布局）和 `MmaDotRule`（MMA 定下 a/b/c/d 四个布局）。

#### 4.2.2 核心流程

以逐元素二元指令 `c = a + b` 为例，`BinaryRule.inference` 的决策树：

```
对 AddInst(a, b) -> c：
  若 a、b、c 三者全部无布局      → 返回 {}（交给种子规则或下一轮）
  若 c 有布局（反向）            → a = c.layout.reduce_to(a.shape)
                                   b = c.layout.reduce_to(b.shape)
  若 c 无布局但 a 有且 a.shape==c.shape（前向） → c = a.layout
  若 c 无布局但 b 有且 b.shape==c.shape（前向） → c = b.layout
  否则                           → 返回 {}
```

直观理解：加减乘除这种逐元素运算，结果布局「跟随」任意一个已定的操作数；谁先有布局就听谁的，且通过 `reduce_to` 处理形状不一致（广播）的情况。

#### 4.2.3 源码精读

**逐元素二元（前向 + 反向俱全）**：

[`inference_rules/elementwise_binary.py:28-50`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/inference/inference_rules/elementwise_binary.py#L28-L50) —— `BinaryRule` 同时覆盖两种方向：

- 第 37-44 行是**反向**：`c.optional_layout is not None` 时，用 `c.layout.reduce_to(a.shape)` 给 `a`、`b` 补布局。`reduce_to` 的作用是把「大形状的布局」收缩到「小形状」（把多余维度的 spatial mode 改成复制，见 u4-l4），从而正确处理广播。
- 第 45-46 行与 47-48 行是**前向**：`a`（或 `b`）已有布局且形状与 `c` 相同时，直接 `{c: a.layout}`。

**逐元素一元（最简洁的前/反向）**：

[`inference_rules/elementwise_unary.py:23-36`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/inference/inference_rules/elementwise_unary.py#L23-L36) —— `UnaryRule` 处理 `CastInst` 等：`x` 有布局则 `{y: x.layout}`（前向），`y` 有布局则 `{x: y.layout}`（反向）。因为是一元运算，形状必然相同，连 `reduce_to` 都不需要。

**种子规则①：全局加载**：

[`inference_rules/ldst_global.py:25-88`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/inference/inference_rules/ldst_global.py#L25-L88) —— `LoadStoreGlobalRule.inference` 是最重要的种子之一。当寄存器张量尚无布局时（第 40-42 行），它**不依赖任何已知布局**，而是：

1. 第 67-68 行：用 `analyze_grid` 分析偏移表达式与掩码表达式，得到每一维的「整除性 / 连续性 / 常量性」（依赖 `ctx.analysis`，即标量分析结果）；
2. 第 71-80 行：对每一维算一个向量化因子 `factor`，取这些性质与 `128 // dtype.nbits`（一个 128 位事务能装几个元素）、维度大小、`max_factor` 的 **gcd**；
3. 第 81-88 行：据此用 `auto_local_spatial(...).local(*rhs_shape)` 构造一个「跨线程分布 + 局部连续」的布局，让连续元素落在同一线程以支持向量化访存。

这条规则完美体现了「布局由硬件友好的访存模式反推」的思想——读者第一次看到「为什么 load 出来的寄存器张量是这种排布」的根因就在这里。

**种子规则②：MMA 点积**：

[`inference_rules/mma_dot.py:27-40`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/inference/inference_rules/mma_dot.py#L27-L40) —— `MmaDotRule.generate_default_layouts` 当 `a/b/c/d` 全部无布局时，调用 `cuda.resolve_dot_config(...)` 查到一组与硬件 MMA 指令匹配的原子布局 `{a: mma.la, b: mma.lb, c: mma.lc, d: mma.lc}`，**一次性给四个张量定布局**。这就是 matmul 内核里累加器与两个输入操作数布局的最终来源。

[`inference_rules/mma_dot.py:66-95`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/inference/inference_rules/mma_dot.py#L66-L95) —— 完整的 `inference` 方法还支持**反向**（第 79-93 行）：当输出 `d` 已有布局时（例如被用户 `AnnotateLayoutInst` 显式指定），它从 `AtomicMmaConfig` 里反查匹配的原子配置，再用 `divide` + `local` + `compose` 把 `d` 的布局拆解成 `a`、`b`、`c` 的布局。

> 直觉总结：`load_global` 与 `dot` 是两条「主种子」，它们定下的布局会经由 `BinaryRule`、`UnaryRule`、`AssignRule` 等逐元素规则**前向/反向扩散**到整张数据流图，最终覆盖所有寄存器张量。

#### 4.2.4 代码实践

**实践目标**：为 `AddInst` 推理规则画出输入输出布局传播方向，并用 `mma_dot` 规则对照理解 MMA 默认布局的生成（即本讲指定的实践任务）。

**操作步骤**（源码阅读 + 画图型）：

1. 阅读 [`inference_rules/elementwise_binary.py:28-50`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/inference/inference_rules/elementwise_binary.py#L28-L50)，在纸上画一张三元图：节点 `a`、`b`、`c`，分别标出「前向」箭头（`a→c`、`b→c`，条件：形状相同）与「反向」箭头（`c→a`、`c→b`，用 `reduce_to`）。
2. 对照 [`inference_rules/mma_dot.py:73-78`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/inference/inference_rules/mma_dot.py#L73-L78) 的「全部无布局」分支：画一个四元图 `a/b/c/d`，从中心「`resolve_dot_config`」向四个节点各画一条「生成」箭头，标注「种子：凭 dtype+shape+num_warps 生成」。
3. 再对照第 79-93 行的「`d` 有布局」分支：从 `d` 向 `a/b/c` 画三条「反向」箭头。

**需要观察的现象**：`AddInst` 的传播是「单输入 ↔ 单输出」的对称双向；而 `MmaDotRule` 既有「凭空生成」（种子）能力，又有「从 `d` 反推多输入」能力，复杂度远高于逐元素规则。

**预期结果**：得到两张方向图，能指出「matmul 内核里累加器 `c` 的布局最初由 `MmaDotRule.generate_default_layouts` 生成，而非用户指定」。

> 待本地验证：若想眼见为实，可用 `tilus.option.debug.dump_ir()` 跑一个 naive matmul，在缓存目录 `ir/` 下找到 `layout_inference` 相关的 IR 文件，确认 `dot` 指令的操作数在推理前无布局、推理后被填上 MMA 布局。

#### 4.2.5 小练习与答案

**练习 1**：对 `c = a + b`，若 `a` 有布局、`b` 无布局、`c` 无布局，且 `a.shape != c.shape`（发生广播），`BinaryRule` 会怎么推？

> **答案**：走第 45-46 行的前向分支需要 `same_list(a.shape, c.shape)` 为真，此处不满足；第 47-48 行 `b` 也无布局；故返回 `{}`，本轮推不出来，要等 `c` 被别的指令定下布局后再反向用 `reduce_to` 补 `a`、`b`。

**练习 2**：`MmaDotRule` 为什么必须同时给 `c` 和 `d` 都赋成 `mma.lc`？

> **答案**：`DotInst` 语义是 `d = a @ b + c`（原地累加，见 u1-l5），累加器输入 `c` 与输出 `d` 是同一个寄存器张量的「前后状态」，自然共享同一布局（见 `mma_dot.py:40` 的 `{..., c: mma.lc, d: mma.lc}`）。

---

### 4.3 优先级与迭代求解：infer_layout 主循环

#### 4.3.1 概念说明

有了规则，下一步是「**按什么顺序、何时停止**」应用它们。Tilus 采用一个简洁的**不动点迭代（fixpoint iteration）**算法：

- 反复扫描程序，每轮挑一条「能推出新布局」的指令应用规则；
- 每推出一个新布局，就 `rewrite` 整个函数（把旧张量替换成带布局的新张量），然后**从头重新开始下一轮**；
- 直到某轮发现「所有张量都有布局」→ 成功返回；或「没有任何规则能再推出新布局」→ 抛 `LayoutInferenceError`。

为什么每推一个就重启？因为新填入的布局可能让原本「推不出来」的别的指令变得可推（约束传播）。重启虽是 \(O(n^2)\) 量级，但实现极简且正确性一目了然。

为了让「更重要的约束」先被满足（比如硬件 MMA 的固定布局优先于逐元素的自由布局），算法给每条规则排了一个**全局优先级**，每轮按优先级顺序尝试。

#### 4.3.2 核心流程

`infer_layout` 的主循环（对照模块顶部 docstring 的伪代码）：

```
前置检查（step 0）：每条「有缺失布局」的指令都必须注册了推理规则，且该规则在优先级表 rule2order 中；否则直接报错。

while True:
    1. 收集所有指令（含各自所处的线程组上下文 ctx）。
    2. 过滤出「仍有缺失布局」的指令 instructions。
    3. 若 instructions 为空 → 全部布局已就绪，返回 func。
    4. 为每个 (指令, 规则) 对算一个三元排序键，升序排序：
         (是否已有部分布局[0<1], 规则优先级 rule2order, 程序位置 tiebreaker)
    5. 按序遍历，对每个 (指令, 规则) 调用 rule.inference(ctx, inst)：
         - 若返回非空 mapping → 用 rewrite 把新布局写回 func，置 found=True，跳出内层循环（回到 step 1）。
    6. 若一整轮 found 仍为 False → 没有规则能推进，抛 LayoutInferenceError。
```

排序键的三级优先级是本算法的精髓（见下文源码精读）。

#### 4.3.3 源码精读

**优先级表**：所有推理规则在导入时被排成一个嵌套列表，再展平成 `rule2order: {RuleClass: int}`。

[`order.py:56-88`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/inference/order.py#L56-L88) —— `inference_order` 从上到下、从左到右编号递增。可以清楚看到优先级分组：

- 最优先：**TMEM 布局规则**（`Tcgen05Alloc/Slice/Load/Store/MmaSS/MmaTS`，Blackwell 张量内存）；
- 其次：寄存器的 `Slice/SliceAssign/AllocBarrier`，然后是 **`MmaDotRule`**（MMA 种子，优先级很高）；
- 接着 `WgmmaMmaSSRule`、tcgen05 的 load/store/copy；
- 中段：`BinaryRule`/`UnaryRule`（逐元素）、`LoadGlobalRule`（全局加载种子）、`Reduce`、`Scan`、各种 `transform`、`Where`、`Assign`、`StoreGlobal`；
- 末段：`Atomic`/`Scatter`、`clc`/`mapa`、`EmptyRule`，以及最低优先级的**共享内存规则**（`LoadSharedInferSwizzledSharedRule` 等）。

这个顺序的设计直觉是：**硬件约束越强、越「独断」的规则越靠前**（TMEM、MMA 必须用特定布局），而越「随和」的规则（逐元素、共享内存搬运）越靠后——后者愿意接受任何被前者定下的布局。

[`order.py:90-99`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/inference/order.py#L90-L99) —— `init_rule_sort_key`（由 `@initialize()` 在包加载时执行一次）把嵌套列表展平成单调递增的整数序号。

**指令收集与上下文**：

[`inference.py:68-98`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/inference/inference.py#L68-L98) —— `InstructionCollector` 是一个 `IRVisitor`。注意它的 `visit_Function`（第 76-81 行）把整个函数的线程数 `num_warps * 32` 压栈，`visit_ThreadGroupStmt`（第 83-89 行）随线程组嵌套更新栈。于是每条指令在 `visit_Instruction` 里记录到的 `LayoutInferenceContext.num_threads`，反映的是**它实际所处线程组的线程数**——这就是为什么 `single_thread()` 块内的指令，其寄存器张量会被推理成「单线程持有全部元素」的布局。

**主循环**：

[`inference.py:191-217`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/inference/inference.py#L191-L217) —— step 0 前置检查：任何「有缺失布局却没注册规则」或「注册了规则却不在 `rule2order` 里」的指令，都会在这里被收集并一次性报错。这条防线保证后续循环里 `rule2order[rule]` 永不 `KeyError`。

[`inference.py:219-230`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/inference/inference.py#L219-L230) —— while 循环开头：重新收集指令、过滤出仍缺布局者；若已无缺失，直接 `return func`。注意每轮都重新 `InstructionCollector().visit(func)`，因为上一轮的 `rewrite` 产生了全新的张量对象。

[`inference.py:233-255`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/inference/inference.py#L233-L255) —— **三级排序键** `pair_sort_key`，升序排列：

1. **第一级**（第 250 行）：`0 if has_inferred_layouts(instruction) else 1`。即「自身已至少有一个操作数带布局」的指令优先处理（排在前）。因为它们能立即向其它操作数传播；完全没布局的指令要等种子规则。
2. **第二级**（第 251 行）：`rule2order[inference_rule]`，规则的全局优先级。
3. **第三级**（第 252 行）：`len(instructions) - inst2order[inst]`，作为同级候选间的稳定 tiebreaker。

> 前两级是主导。第一级保证「能传播的先传播」，第二级保证「强约束的规则先发声」。两者共同实现了「种子 → 扩散」的求解顺序。

[`inference.py:262-303`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/inference/inference.py#L262-L303) —— step 4 应用规则。对每个 (指令, 规则) 调用 `rule.inference(ctx, inst)`，若返回非空 `mapping`：先做三道断言校验（张量类型、布局类型、`same_list(tensor.shape, layout.shape)`），再用 `with_layout` 生成新张量、组成 `rewrite_map`，最后 `func = rewrite(func, rewrite_map)` 并 `break`（跳出内层 for，回到 while 顶部开启新一轮）。`found = True` 标记本轮有进展。

[`inference.py:304-320`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/inference/inference.py#L304-L320) —— 若一整轮没有任何规则能推进（`found` 仍为 `False`），收集所有仍无布局的张量，连同整个函数的可读文本，抛出 `LayoutInferenceError`。这是推理失败的唯一出口。

#### 4.3.4 代码实践

**实践目标**：理解优先级如何影响求解顺序。

**操作步骤**（思想实验 + 源码阅读型）：

1. 假设一段 IR 里有三条「缺布局」的指令，按程序顺序是：`%r1 = load_global(...)`、`%r2 = cast(%r1)`、`%r3 = add(%r2, %r2)`。初始时三者都无布局。
2. 对照 [`order.py:56-88`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/inference/order.py#L56-L88) 找出三条规则的优先级：`LoadGlobalRule`、`UnaryRule`（cast）、`BinaryRule`（add）。
3. 推演第一轮：三条指令都「完全没有布局」（第一级都是 1），于是按第二级规则优先级排序——`LoadGlobalRule` 最先尝试，它是种子，能凭空生成 `%r1` 的布局 → 应用，重启。
4. 推演第二轮：`%r1` 已有布局，故 `cast(%r1)` 这条指令第一级变成 0（优先），`UnaryRule` 前向把 `%r1.layout` 传给 `%r2` → 应用，重启。
5. 推演第三轮：`%r2` 有布局，`add` 的 `BinaryRule` 前向定 `%r3` → 全部就绪，返回。

**需要观察的现象**：尽管 `add` 在程序里排最后，但求解顺序由「谁先有已知布局 + 规则优先级」决定，而不是程序文本顺序。

**预期结果**：能口述「种子规则 `LoadGlobalRule` 启动推理 → 逐元素规则逐级前向扩散」这条典型链路。

> 待本地验证：用 `dump_ir` 导出某 matmul 各 Pass 后的 IR，对比 `layout_inference` 前后张量布局是否被填充（这是 u5-l2 的实践，本讲可先做思想推演）。

#### 4.3.5 小练习与答案

**练习 1**：为什么共享内存规则（如 `LoadSharedInferRowMajorSharedRule`）被放在 `inference_order` 的最末尾？

> **答案**：共享内存的布局最「灵活」——它只是中转搬运，愿意接受寄存器侧已定下的任何排布（再配 swizzle 消除 bank conflict）。把它放最后，可以让前面的 MMA/load 规则先把「硬约束」布好，共享内存规则再去适配，避免它过早定下一个与 MMA 不兼容的布局而被验证规则拒绝。

**练习 2**：若两条规则优先级相同（同一组内），靠什么决定谁先试？

> **答案**：靠第三级 tiebreaker `len(instructions) - inst2order[inst]`（程序位置），以及内层 for 循环里「一旦有规则成功就 `break`」的「先到先得」语义（见 `inference.py:302-303`）。

---

### 4.4 验证与覆盖：cover 如何把关一致性

#### 4.4.1 概念说明

推理结束后，所有张量都有了布局。但这不代表它们「相容」——比如一条 `add`，若输入 `a` 的布局把元素散布在线程 0-63，而输出 `c` 的布局把同样元素散布在线程 0-31，那这个加法在硬件上根本无法逐线程执行（一个线程拿不到它该算的两个操作数）。

**验证规则**就是事后把关：它检查「按这条指令的语义，输入/输出的线程映射是否兼容」。对逐元素运算，兼容的判据是：**输出的线程映射必须被输入的线程映射「覆盖」**——即「持有输出某元素的线程，必然也持有计算它所需的输入元素」。这个「覆盖」关系由 `MultiFunction.cover` 实现（u4-l4 已介绍 `cover`/`collapse` 是布局等价性工具）。

#### 4.4.2 核心流程

验证在 Pass 层、推理完成后统一执行一次：

```
infer_layout(func)  →  所有张量已有布局
        │
        ▼
verify_layouts(func):
  对每条含寄存器/共享张量的指令：
      rule = get_validation_rule(inst)
      ok = rule.validate(inst)        # 例如用 cover 检查线程映射覆盖关系
      if not ok: 记入 invalid_instructions
  返回 invalid_instructions
        │
        ▼
若 invalid_instructions 非空 → LayoutInferencePass 抛 LayoutInferenceError（附整段 IR 与出错指令的布局）
```

#### 4.4.3 源码精读

**逐元素二元的验证（`cover` 的典型用法）**：

[`validation_rules/elementwise_binary.py:28-41`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/inference/validation_rules/elementwise_binary.py#L28-L41) —— `BinaryRule.validate` 对两个输入分别检查：

- 第 36 行：`fa = ops.identity(y.shape).collapse_by_shape(x.shape) * x.layout.spatial_mfunction()`。先把输出的「恒等线程映射」`identity(y.shape)` 按输入形状 `collapse`（塌缩掉广播维），再与输入 `x` 的 `spatial_mfunction`（逻辑索引→线程号）复合。
- 第 37 行：`fb = y.layout.spatial_mfunction()`，输出的线程映射。
- 第 39 行：`if not fa.cover(fb): return False`。

直观读法：「输出 `y` 的线程分布 `fb`，必须能被「输入 `x` 的线程分布经形状适配后的」映射 `fa` 覆盖」。否则存在某些输出元素，持有它的线程在输入侧拿不到对应数据，验证失败。

> `spatial_mfunction` 的定义见 [`register_layout.py:114-120`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/register_layout.py#L114-L120)，它返回「全局索引→（串行化的）spatial 索引」的 MultiFunction；`cover` 的语义见 [`mfunction.py:193-197`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/mfunction/mfunction.py#L193-L197)。

**赋值的验证（最简形式）**：

[`validation_rules/assign.py:21-30`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/inference/validation_rules/assign.py#L21-L30) —— `AssignRule.validate` 直接 `fa.cover(fb)`：源张量 `x` 的线程映射必须覆盖目标 `y` 的线程映射。赋值是逐元素拷贝，要求两个张量在同一线程手里持有对应元素。

**验证的调用入口**：

[`inference.py:323-345`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/inference/inference.py#L323-L345) —— `verify_layouts(func)` 遍历所有指令，对「含寄存器/共享张量」的指令取其验证规则并 `validate`，收集所有非法指令返回。注意它**只收集不抛错**——真正抛错的决定权在 Pass。

**Pass 把推理与验证串起来**：

[`transforms/layout_inference.py:69-100`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/layout_inference.py#L69-L100) —— `LayoutInferencePass.process_function` 三步走：

1. 第 71-72 行：先用 `ApplyLayoutAnnotationRewriter` 应用用户手写的 `AnnotateLayoutInst`（把用户显式指定的布局 `with_layout` 到张量上，作为推理的「硬种子」）；
2. 第 73 行：`func = infer_layout(func)` 跑不动点推理；
3. 第 74 行：`self.verify_layouts(func)` 跑验证，若有非法指令就抛带完整上下文的 `LayoutInferenceError`（第 82-99 行拼出出错指令及其布局，非常便于调试）。

**用户注解机制**：

[`transforms/layout_inference.py:31-56`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/layout_inference.py#L31-L56) —— `visit_AnnotateLayoutInst` 把用户通过 `AnnotateLayoutInst` 指定的布局绑定到张量。若张量已有**不同**布局会抛错（第 38-41 行），保证用户注解与自动推理不冲突。这是 Tilus 提供「半自动」布局控制的逃生口：用户可钉死个别张量的布局，其余交给推理。

#### 4.4.4 代码实践

**实践目标**：理解 `cover` 在验证中的角色。

**操作步骤**（源码阅读型）：

1. 阅读 [`validation_rules/elementwise_binary.py:36-40`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/inference/validation_rules/elementwise_binary.py#L36-L40)，回答：如果 `a` 和 `c` 形状相同、但 `a` 用了「每线程 4 元素连续」布局、`c` 用了「每线程 1 元素」布局，`fa.cover(fb)` 大概率成立还是不成立？
2. 对照 [`transforms/layout_inference.py:82-99`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/layout_inference.py#L82-L99)，看懂出错时拼出的错误信息结构（整段 IR + 每条非法指令 + 其张量布局）。

**需要观察的现象**：`cover` 判定的不是「布局是否相等」，而是「线程映射是否存在包含关系」——这是一个比「严格相等」宽松、但足以保证可执行性的条件。

**预期结果**：能口头解释「为什么逐元素验证用 `cover` 而不是 `==`」：因为广播/复制语义下，输入布局可以比输出布局「更分散」（更多线程持有同一元素），只要覆盖输出即可。

> 待本地验证：可尝试构造一个 `AddInst`，人为给两个操作数不兼容的布局，调用 `verify_layouts` 观察它是否被收入 `invalid_instructions`（需要按 `tests/transforms/` 的风格手工构造 IR，可参照 u5-l3 的测试范式）。

#### 4.4.5 小练习与答案

**练习 1**：验证规则用 `cover` 而非 `==` 判等，本质原因是什么？

> **答案**：逐元素运算允许广播与复制（replicated）布局——同一输入元素可被多个线程持有。只要「持有输出元素的线程，也持有它需要的输入元素」即可正确执行，这恰是 `fa.cover(fb)` 的语义；用 `==` 会误杀合法的广播/复制情形。

**练习 2**：`AlwaysOkayRule` 覆盖了大量指令（如 `LoadSharedInst`、`StoreGlobalInst`），这意味着这些指令的布局「随便什么都行」吗？

> **答案**：不完全是。它意味着**这些指令自身不施加额外约束**——它们的布局合法性已经由「别的指令」的推理与验证间接保证（例如 `store_global` 读的寄存器张量，其布局已由 `load_global` 或 `dot` 定下并通过逐元素链路验证）。`AlwaysOkayRule` 只是说「我这道关卡不再额外挑剔」。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个「**从种子到覆盖**」的小任务。

### 实践目标

用一个真实内核观察布局推理的完整效果，并在纸上为 `AddInst` 与 `DotInst` 分别画出传播方向图。

### 操作步骤

1. **准备环境**（参照 u1-l2 与 u3-l1）：

   ```python
   # 示例代码（非项目原有代码）
   import tilus
   tilus.option.cache_dir("/tmp/tilus-cache-u4l5")   # 指定一个干净的缓存目录
   tilus.option.debug.dump_ir()                       # 开启逐 Pass IR 落盘
   ```

2. **跑一个 naive matmul**（直接用 `examples/matmul/matmul_v0.py` 的入口，或你自己在 u1-l5 写的版本），用 `torch` 校验结果正确。

3. **到缓存目录 `/tmp/tilus-cache-u4l5` 下**，找到对应 `programs/<hash>/ir/` 目录。其中会有按 Pass 顺序导出的 IR 文件。定位到 `layout_inference` 这一阶段**前后**的两个文件（注意 `get_default_passes` 里 `layout_inference_pass` 出现了两次，见下方说明）。

4. **对比观察**：
   - 推理前：`register_tensor`、累加器等张量的布局应为 `None`（或未标注）。
   - 推理后：`dot` 指令的 `a/b/c/d` 四个张量应被填上 MMA 布局（来自 `MmaDotRule.generate_default_layouts`）；收尾的 `cast`/`store_global` 链路上的张量布局应经由 `UnaryRule` 前向扩散而就绪。

5. **画图任务**（本讲指定的核心实践）：
   - 为 `AddInst`（`c = a + b`）画传播方向图：标出前向（`a→c`/`b→c`，形状相同时直接复制布局）与反向（`c→a`/`c→b`，用 `reduce_to`）。
   - 为 `DotInst`（`d = a @ b + c`）画图：标出「种子生成」（四元全无布局时由 `resolve_dot_config` 生成）与「反向」（`d` 有布局时反推 `a/b/c`）。
   - 在两张图上用箭头颜色区分「前向 / 反向 / 种子生成」。

### 需要观察的现象

- `layout_inference` 前后，张量的 `optional_layout` 从无到有；
- MMA 相关张量的布局带有明显的 `spatial`/`local` mode 结构（即「跨线程分布 + 线程内连续」）；
- 第二次 `layout_inference_pass`（在 `lower_load_store` 之后）主要处理 lowering 引入的新共享内存搬运张量，这也解释了为何 Pass 出现两次。

### 预期结果

- 得到一份能跑通、结果正确的 matmul；
- 缓存目录里能看到推理前后的 IR 对比；
- 两张标注清晰的传播方向图，能指着图讲清「种子 → 前向/反向扩散 → 验证」全流程。

> 待本地验证：本实践依赖一块 Tilus 支持的 GPU（Ampere/Hopper/Blackwell）。若无 GPU，可退化为「源码阅读型实践」：直接读 `inference_rules/mma_dot.py` 与 `inference_rules/elementwise_binary.py`，在纸上完成画图与方向标注，并用 `order.py` 的优先级表推演求解顺序。

---

## 6. 本讲小结

- **两套规则**：推理规则（`LayoutInferenceRule`，可多挂）负责「补」布局，验证规则（`LayoutValidationRule`，唯一）负责「查」布局；二者都通过 `@register_rule(InstCls)` 注册，查询时沿 `__mro__` 自动继承父类规则。
- **三种传播方向**：前向（输入→输出，如 `UnaryRule`）、反向（输出→输入，如 `BinaryRule` 用 `reduce_to`）、以及凭空生成的**种子**（`LoadGlobalRule` 凭访存模式、`MmaDotRule` 凭 `resolve_dot_config`）。
- **不动点迭代**：`infer_layout` 反复扫描，每轮按「是否已有部分布局 → 规则全局优先级 → 程序位置」三级排序，挑第一条能推出新布局的规则应用并重启；推不动则报错。
- **优先级即约束强度**：`rule2order` 把 TMEM/MMA 等硬件强约束排在前，逐元素/共享内存搬运排在后，让「独断」的规则先发声。
- **验证用 `cover`**：逐元素验证不强求布局相等，而用 `MultiFunction.cover` 判断「输出线程映射被输入覆盖」，从而正确接纳广播与复制。
- **Pass 出现两次**：`layout_inference_pass` 在 `get_default_passes` 里位于 `lower_load_store` 前后各一次——前者推理原始张量，后者推理 lowering 新引入的共享内存张量。

---

## 7. 下一步学习建议

本讲结束了 u4「布局系统」单元。到这里你已经掌握了「布局是什么、怎么运算、怎么被自动推理出来」。接下来：

1. **u5-l1 Pass 框架与 IRRewriter/IRVisitor**：本讲的 `LayoutInferencePass` 就是 `Pass` 基类的一个实例；学完 Pass 框架你会更清楚 `process_function`、`apply_transforms` 的来龙去脉，以及 `InstructionCollector` 所用的 `IRVisitor` 与 `IRRewriter` 的区别。
2. **u5-l2 默认变换流水线 `get_default_passes`**：把本讲提到的「`layout_inference_pass` 为何出现两次」放到完整流水线里理解，看清 `lower_load_store` 与两次推理的配合。
3. **u5-l3 死代码消除与标量分析**：理解 `ctx.analysis`（整除性/上下界）是怎么由 `analyze_scalar_pass` 产出的——它正是 `LoadGlobalRule` 决定向量化宽度的依据。
4. **动手实验**：试着用 `AnnotateLayoutInst`（即用户布局注解）钉死一个 matmul 累加器的布局，观察推理与验证是否如你预期地接受或拒绝；这会加深你对「半自动布局控制」与 `cover` 的直觉。
