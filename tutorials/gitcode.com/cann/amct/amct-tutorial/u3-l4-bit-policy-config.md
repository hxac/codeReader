# BitPolicy 位宽配置与 yaml 模板

## 1. 本讲目标

本讲解决一个问题：**量化时，模型里那么多 Linear 层，每一层该用几位？这个决定存在哪里、怎么被解析？**

读完本讲你应该能够：

- 看懂 `configs/` 目录下任意一份 `bit_config` yaml，说出全局默认位宽、分组覆盖、逐层覆盖分别在哪里。
- 说清 `BitPolicy.linear_bits()` 的「从最具体叶子到最粗组」逐级回退解析规则，并能手算任意一层的最终 `(w_bits, a_bits)`。
- 写出一份自定义 `bit_config`，用 `BitPolicy.from_yaml` 加载并用 `summary()` 验证。
- 理解 `_GroupBits` 下标代理如何让下游代码用 `bit_policy["mlp"]["down_proj"].w` 这样自然的方式取位宽。

本讲是 [u3-l1 CLI 参数体系](u3-l1-cli-args.md) 的延续：u3-l1 讲到 `--bit_config` 经 `BitPolicy.from_yaml` 变成 `args.bit_policy`，本讲就钻进这个对象内部。

## 2. 前置知识

### 2.1 为什么需要「逐层不同」的位宽

回顾 [u2-l2 量化数据类型全览](u2-l2-quant-dtypes-overview.md)：量化的体积收益只由位宽决定（8-bit↓50%、4-bit↓75%）。照理把所有层都压到最低位最省体积，但**不同层对量化的敏感度不同**：

- **down_proj**（MLP 的输出投影）和 **o_proj**（Attention 的输出投影）往往要聚合更多通道的激活，outlier 多，压太狠精度掉得快。
- **gate_proj / up_proj** 相对鲁棒，可以压得更低。
- **MoE**（混合专家）里 routed expert（被路由命中的专家）和 shared expert（所有 token 共享的专家）职责不同，敏感度也不同。

所以工程上常见的需求是：**全局一个默认位宽，但对个别敏感层单独放宽，对个别不敏感层单独压低。** `BitPolicy` 就是表达这套「分层位宽策略」的对象。

### 2.2 w_bits 与 a_bits 是什么

- `w_bits`（weight bits）：**权重**的量化位宽。权重是静态的，可以离线量化（见 [u2-l1](u2-l1-compression-basics.md)）。
- `a_bits`（activation bits）：**激活**的量化位宽。激活随输入变化，需要校准数据估计范围。

一个层可以「权重压低、激活保持」（W4A16，省带宽），也可以「权重激活都压」（W8A8，省算力）。所以位宽是**一对** `(w_bits, a_bits)`，不是一个数。这也是后面「校验时 w/a 必须成对出现」的根本原因。

### 2.3 AMCT 允许的位宽只有三种

记住常量 `_ALLOWED_BITS = (4, 8, 16)`。其中 `16` 表示「不量化、保持浮点（bf16/fp16）」。所以一份配置里出现 `16`，意思是「这一块先别动」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [amct_pytorch/quantization/bit_policy.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/bit_policy.py) | 核心实现。`BitPolicy` 类、`_GroupBits` 代理、`LayerBits` 元组、`ensure_bit_policy` 辅助、四个校验函数。 |
| [amct_pytorch/configs/w8a8.yaml](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/configs/w8a8.yaml) | 最简模板：全局 W8A8，无任何覆盖。 |
| [amct_pytorch/configs/w4a8.yaml](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/configs/w4a8.yaml) | 全局 W4A8，仅对 moe 的 routed/shared 做分组覆盖。 |
| [amct_pytorch/configs/example_w4a8.yaml](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/configs/example_w4a8.yaml) | 教学样例：全局 W4A8，覆盖 attn-linear / mlp / moe.routed / moe.shared / attn-cache 五种写法。 |
| [amct_pytorch/configs/bf16.yaml](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/configs/bf16.yaml) | 全 16 的基线配置（只有注释，体现代码「缺省即 16」的设计）。 |

下游消费方只看一处：[amct_pytorch/common/models/llm/common/quant_apply.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/quant_apply.py) 里的 `QuantGatedMLP`，用它说明位宽最终怎么被取走。

---

## 4. 核心概念与源码讲解

### 4.1 BitPolicy 是什么：位宽策略的内存表示

#### 4.1.1 概念说明

`BitPolicy` 是 `--bit_config` yaml 文件加载到内存后的对象。它做的事情很专一：

1. **存**：把 yaml 的嵌套字典原样保存在 `self.cfg` 里。
2. **查**：提供 `linear_bits(name, group)` 方法，按「逐级回退」规则回答「这一层用几位」。
3. **校验**：在加载时确保位宽取值合法、w/a 成对出现。

它**不做量化本身**，只是「位宽策略的查询服务」。真正的量化器（`WeightQuantizer` / `ActivationQuantizer`）在构造时从它这里取走 `(w_bits, a_bits)`，之后就不再依赖它。

#### 4.1.2 核心流程：一份 yaml 的结构

先建立 yaml 的整体骨架。以 `example_w4a8.yaml` 为例，结构分四层：

```text
顶层全局默认          w_bits / a_bits
  ├── 分组 group        attn-linear / mlp / moe / attn-cache
  │     ├── 子分组       moe 下有 routed / shared
  │     └── 叶子 leaf     匹配真实层名：q_proj / o_proj / down_proj ...
  └── attn-cache 特殊    扁平的单值字典（k / v / q / p），不是 w/a 对
```

四个常量定义了合法的分组名，见 [amct_pytorch/quantization/bit_policy.py:L22-L25](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/bit_policy.py#L22-L25)：

```python
_BIT_KEYS = ("w_bits", "a_bits")
_ALLOWED_BITS = (4, 8, 16)
_LINEAR_GROUPS = ("attn-linear", "mlp", "moe")
_CACHE_GROUP = "attn-cache"
```

注意 `_LINEAR_GROUPS` 只有三项，`attn-cache` 单独放在 `_CACHE_GROUP`——因为 cache 的位宽是**单值**（一个 KV cache 只有一个位宽概念，不分权重和激活），而 linear 三组的位宽是 **`(w_bits, a_bits)` 一对**。这是 `BitPolicy` 内部最重要的区分。

位宽对用一个轻量 `namedtuple` 表示，见 [amct_pytorch/quantization/bit_policy.py:L20](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/bit_policy.py#L20)：

```python
LayerBits = namedtuple("LayerBits", ("w", "a"))
```

这样下游就能用 `gate.w`、`down.a` 这种可读字段访问，而不是 `tuple[0]`。

#### 4.1.3 源码精读：构造与默认值

`BitPolicy.__init__` 见 [amct_pytorch/quantization/bit_policy.py:L49-L59](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/bit_policy.py#L49-L59)：

```python
def __init__(self, cfg: dict | None = None):
    self.cfg = cfg or {}
    if ("w_bits" in self.cfg) != ("a_bits" in self.cfg):
        ...  # 顶层 w/a 必须成对，否则抛 ValueError
    self.w_bits = int(self.cfg.get("w_bits", 16))
    self.a_bits = int(self.cfg.get("a_bits", 16))
    self._validate()
```

两个要点：

1. **缺省即 16**：`self.cfg.get("w_bits", 16)`。yaml 里不写 `w_bits`，就等价于写 `w_bits: 16`（不量化）。`bf16.yaml` 整个文件只有注释、`yaml.safe_load` 返回 `None`，经 `cfg or {}` 变成空字典，于是顶层 `w_bits = a_bits = 16`——这就是「空配置 = 全 fp16 基线」的实现。
2. **顶层 w/a 成对约束**：用 `!=` 比较「两个键是否都在」的布尔值。若只写 `w_bits: 8` 而漏了 `a_bits`，`(True) != (False)` 为真，立刻报错。这避免「只指定了权重位宽、激活被静默当 16」的隐蔽错误。

#### 4.1.4 代码实践：读懂四份模板

1. **实践目标**：建立「yaml 结构 → 位宽语义」的直觉。
2. **操作步骤**：依次打开 `w8a8.yaml`、`w4a8.yaml`、`example_w4a8.yaml`、`bf16.yaml`，对照上面的四层骨架标注。
3. **需要观察的现象**：
   - `w8a8.yaml` 只有顶层两行，没有任何分组——表示「所有层一律 W8A8」。
   - `bf16.yaml` 没有任何键——表示「全 16，不量化」。
   - `w4a8.yaml` 多了 `moe.routed` / `moe.shared` 两个子分组——演示「分组级覆盖」。
   - `example_w4a8.yaml` 五种写法都有——是后面的主样例。
4. **预期结果**：能口述「`bf16.yaml` 为什么等价于全 16」——因为 `__init__` 的 `get(..., 16)` 默认值。

#### 4.1.5 小练习与答案

**练习**：`w8a8.yaml` 里写了 `w_bits: 8` 和 `a_bits: 8`。如果用户把它改成只留 `w_bits: 8`、删掉 `a_bits`，加载时会发生什么？

**答案**：`__init__` 第 51 行的成对校验会触发，抛出 `ValueError: Incomplete bit entry at top level: must set both 'w_bits' and 'a_bits', got ['w_bits'].`。顶层必须成对。

---

### 4.2 BitPolicy.from_yaml 与三层校验

#### 4.2.1 概念说明

`from_yaml` 是把磁盘上的 yaml 文件变成 `BitPolicy` 对象的入口。它在构造对象前后安排了**三层校验**，确保「错的配置尽早暴露、绝不静默通过」：

| 层次 | 校验内容 | 触发位置 | 函数 |
| --- | --- | --- | --- |
| ① 结构层 | 顶层必须是 dict | `from_yaml` 构造前 | `from_yaml` 内联 |
| ② 取值层 | 所有叶子值必须是 int 且 ∈ {4,8,16} | `from_yaml` 构造前 | `_validate_bit_config` |
| ③ 完整性层 | 任何嵌套节点提到 w/a 之一就必须两个都有 | 构造时 `_validate` | `_check_complete` |

#### 4.2.2 核心流程

```text
from_yaml(path)
  ├── yaml.safe_load(path) → cfg
  ├── cfg 是 None?  → 当作 {}（允许空文件）
  ├── cfg 不是 dict? → 报错（结构层 ①）
  ├── _validate_bit_config(cfg)        → 递归校验取值（②）
  └── BitPolicy(cfg)
        ├── __init__: 顶层 w/a 成对 + get 默认 16
        └── _validate → _check_complete 递归校验嵌套成对（③）
```

#### 4.2.3 源码精读

`from_yaml` 本体见 [amct_pytorch/quantization/bit_policy.py:L64-L73](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/bit_policy.py#L64-L73)：

```python
@classmethod
def from_yaml(cls, path: str) -> "BitPolicy":
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    if not isinstance(cfg, dict):
        raise ValueError(f"bit_config yaml at {path} must be a mapping at top level")
    _validate_bit_config(cfg)
    return cls(cfg)
```

注意 `yaml.safe_load(f) or {}`：`safe_load` 对「只含注释的文件」返回 `None`，`or {}` 把它兜成空字典——这就是 `bf16.yaml` 能合法加载的原因。

**第二层：取值校验**。`_validate_bit_config` 递归遍历整棵字典，对每个非 dict 的叶子调 `_validate_bit_value`，见 [amct_pytorch/quantization/bit_policy.py:L175-L188](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/bit_policy.py#L175-L188)：

```python
def _validate_bit_value(path: str, value):
    if type(value) is not int:
        raise ValueError(f"{path} must be int, ...")
    if value not in _ALLOWED_BITS:
        raise ValueError(f"{path} must be one of {_ALLOWED_BITS}, but got {value}.")
```

两个细节：
- 用 `type(value) is not int` 而非 `isinstance`——故意拒绝 `bool`（Python 里 `bool` 是 `int` 子类，`True == 1`），避免 yaml 里写 `w_bits: true` 被静默接受成 `1`。
- 报错信息带上完整点分路径（如 `moe.shared.w_bits`），方便定位。

**第三层：完整性校验**。`_validate` 只检查三个 `_LINEAR_GROUPS`，对每个分组调 `_check_complete`，见 [amct_pytorch/quantization/bit_policy.py:L129-L136](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/bit_policy.py#L129-L136) 与 [amct_pytorch/quantization/bit_policy.py:L159-L172](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/bit_policy.py#L159-L172)：

```python
def _check_complete(node: dict, path: str):
    has_w = "w_bits" in node
    has_a = "a_bits" in node
    if has_w != has_a:
        raise ValueError(f"Incomplete bit entry at {path!r}: ...")
    for k, v in node.items():
        if k in _BIT_KEYS:
            continue
        if isinstance(v, dict):
            _check_complete(v, f"{path}.{k}")
```

关键理解：**完整性校验是「就近配对」，不是「全树配对」**。它的规则是——任何一个嵌套 dict 节点，只要它**自己**提到了 `w_bits` 或 `a_bits` 之一，就必须同时提到另一个。这保证「凡是被显式写出来的位宽，一定是完整的一对」，不会出现「mlp.down_proj 只写了 w_bits」这种半截配置。

> 为什么顶层不放进 `_check_complete`？因为顶层在 `__init__` 里已经单独校验过（第 51 行）。两处规则一致，只是分开了。

#### 4.2.4 代码实践：故意写错触发三层校验

1. **实践目标**：用三份「故意写错」的 yaml 亲历三层报错。
2. **操作步骤**（示例代码，非项目原有文件）：

   ```yaml
   # bad1.yaml —— 取值非法
   w_bits: 6
   a_bits: 8
   ```

   ```yaml
   # bad2.yaml —— 类型错误（写成字符串）
   w_bits: 8
   a_bits: "8"
   ```

   ```yaml
   # bad3.yaml —— 嵌套成对缺失
   w_bits: 4
   a_bits: 8
   mlp:
     down_proj:
       w_bits: 4      # 漏了 a_bits
   ```

   用一行 Python（示例代码）逐个加载：

   ```python
   from amct_pytorch.quantization.bit_policy import BitPolicy
   BitPolicy.from_yaml("bad1.yaml")  # 期望：取值层报 6 不在 (4,8,16)
   BitPolicy.from_yaml("bad2.yaml")  # 期望：取值层报 "8" 不是 int
   BitPolicy.from_yaml("bad3.yaml")  # 期望：完整性层报 mlp.down_proj 缺 a_bits
   ```

3. **需要观察的现象**：每条报错的 `path` 字段（`w_bits` / `a_bits` / `mlp.down_proj`）是否与出错位置对应。
4. **预期结果**：三条命令分别在第②、第②、第③层抛出 `ValueError`，且报错路径精确到节点。具体报错文本**待本地验证**（依你本地的 amct 版本，措辞可能与本讲引用的源码一致）。

#### 4.2.5 小练习与答案

**练习**：`bad2.yaml` 里 `a_bits: "8"` 是字符串。为什么 AMCT 用 `type(value) is not int` 而不是 `isinstance(value, int)` 来拒绝它？

**答案**：主要是为了同时拒绝 `bool`。在 Python 中 `bool` 是 `int` 的子类，`isinstance(True, int)` 为 `True`，于是 yaml 里的 `w_bits: true` 会被 `isinstance` 放行并当成 `1`。用 `type(value) is not int` 严格匹配类型，`bool` 和 `str` 都会被拒。

---

### 4.3 linear_bits 逐级回退解析

#### 4.3.1 概念说明

这是 `BitPolicy` 的**算法核心**，也是它区别于「普通嵌套字典」的地方。

问题：当下游问「MLP 的 `down_proj` 用几位」时，配置里可能在三个地方给过答案——顶层全局、`mlp` 分组、`mlp.down_proj` 叶子。该听谁的？

`linear_bits` 的规则是：**从最具体的叶子开始，逐级向粗回退，返回第一个「w/a 成对完整」的节点；都没有就回退到顶层全局。**

这就允许一种很优雅的写法：

- 想给整组统一改位宽，就在分组节点写一对 `w_bits/a_bits`（组级默认）。
- 想给某层单独改，就在叶子节点写一对（覆盖组级）。
- 不写的层，自动继承组级或顶层。

#### 4.3.2 核心流程：链路构造 + 逆序回退

`linear_bits` 见 [amct_pytorch/quantization/bit_policy.py:L92-L119](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/bit_policy.py#L92-L119)。分两步：

```text
第一步：沿 dotted 路径「向下走」，收集每层节点到 chain
   parts = group.split(".") + [name]      # 如 ["moe","shared","gate_proj"]
   cursor = cfg
   for part in parts:
       sub = cursor.get(part)
       if sub 不是 dict: break            # 路径断了就停
       chain.append(sub); cursor = sub

第二步：逆序「向上回退」，返回第一个完整节点
   for node in reversed(chain):          # 先叶子，后根
       if "w_bits" in node and "a_bits" in node:
           return (node["w_bits"], node["a_bits"])
   return self.w_bits, self.a_bits        # 都没有 → 顶层全局
```

两个关键点：

1. **chain 只装「路径上真实存在的 dict 节点」**。一旦某个 part 取不到 dict（比如 `mlp` 下没有 `gate_proj`），循环就 `break`，后面更深的 part 不再尝试。这保证我们只在「真实存在的祖先链」上回退。
2. **回退时认「成对完整」的节点**。一个分组节点可能只放了一个叶子子项、自己没写 `w_bits/a_bits`——它会被跳过，继续向上找。只有自己显式写了完整一对的节点才会命中。

#### 4.3.3 源码精读：用 example_w4a8 手算五个案例

把 [amct_pytorch/configs/example_w4a8.yaml:L34-L59](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/configs/example_w4a8.yaml#L34-L59) 的关键部分摘出来：

```yaml
w_bits: 4
a_bits: 8

attn-linear:
  o_proj:       { w_bits: 8, a_bits: 16 }

mlp:
  down_proj:    { w_bits: 4, a_bits: 16 }

moe:
  routed:
    down_proj:  { w_bits: 4, a_bits: 16 }
  shared:
    w_bits: 8        # 组级默认（成对写在下一行）
    a_bits: 8
```

逐个推演（`group` 用 dotted 形式，对应下游 `QuantGatedMLP` 构造时传入的 `group` 参数）：

| 查询（name, group） | parts | chain 收集到的节点 | 逆序命中的第一个完整节点 | 结果 |
| --- | --- | --- | --- | --- |
| `o_proj`, `attn-linear` | `[attn-linear, o_proj]` | `[attn-linear, o_proj]` | o_proj（有 8/16） | **(8, 16)** |
| `down_proj`, `mlp` | `[mlp, down_proj]` | `[mlp, down_proj]` | down_proj（有 4/16） | **(4, 16)** |
| `gate_proj`, `mlp` | `[mlp, gate_proj]` | `[mlp]`（mlp 无 gate_proj → break） | mlp 无 w/a → 跳过 → 回退顶层 | **(4, 8)** |
| `down_proj`, `moe.routed` | `[moe, routed, down_proj]` | `[moe, routed, down_proj]` | down_proj（有 4/16） | **(4, 16)** |
| `gate_proj`, `moe.shared` | `[moe, shared, gate_proj]` | `[moe, shared]`（shared 无 gate_proj → break） | shared 有 8/8 → 命中 | **(8, 8)** |

最后一行最值得体会：`moe.shared` 这个分组节点自己写了 `w_bits:8/a_bits:8`，于是**它下面所有没有单独配置的层（gate_proj/up_proj/down_proj）都自动继承 W8A8**。这就是「组级默认」的威力——不用逐层重复写。

对比看 `w4a8.yaml` 的简化版写法，见 [amct_pytorch/configs/w4a8.yaml:L17-L27](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/configs/w4a8.yaml#L17-L27)：全局 W4A8，routed 保持 W4A8，shared 提升到 W8A8——仅靠两个分组级条目就表达完整，没有任何叶子覆盖。

#### 4.3.4 代码实践：写一份自定义 bit_config 并验证解析

1. **实践目标**：综合运用「全局默认 + 叶子覆盖 + 回退规则」。任务——全局 W8A8，但把 `attn-linear` 的 `o_proj` 提升到 W16A16、`mlp` 的 `down_proj` 设为 W4A8。
2. **操作步骤**：

   写文件 `my_bits.yaml`（示例代码）：

   ```yaml
   w_bits: 8
   a_bits: 8

   attn-linear:
     o_proj: { w_bits: 16, a_bits: 16 }

   mlp:
     down_proj: { w_bits: 4, a_bits: 8 }
   ```

   用 Python 加载并探针（示例代码）：

   ```python
   from amct_pytorch.quantization.bit_policy import BitPolicy

   bp = BitPolicy.from_yaml("my_bits.yaml")
   print(bp.summary())                          # 打印整份配置

   # 探针：直接调 linear_bits 验证回退
   print(bp.linear_bits(name="o_proj",    group="attn-linear"))  # 期望 (16, 16)
   print(bp.linear_bits(name="q_proj",    group="attn-linear"))  # 期望 (8, 8) —— 回退顶层
   print(bp.linear_bits(name="down_proj", group="mlp"))          # 期望 (4, 8)
   print(bp.linear_bits(name="gate_proj", group="mlp"))          # 期望 (8, 8) —— 回退顶层
   print(bp.has_quant_linear())                 # 期望 True（有 <16 的条目）
   ```

3. **需要观察的现象**：
   - `summary()` 是否原样回显了 yaml 内容。
   - 四个 `linear_bits` 探针的返回是否符合上表的手算规则。
   - `q_proj` / `gate_proj` 这种「yaml 里没写」的层，是否正确回退到顶层 `(8, 8)`。
4. **预期结果**：`summary()` 回显配置；四个探针依次返回 `(16,16)`、`(8,8)`、`(4,8)`、`(8,8)`；`has_quant_linear()` 返回 `True`。若你的本地环境尚未安装 amct，可用 `python -c "import yaml,sys; ..."` 单独拷贝 `bit_policy.py` 逻辑验证（该文件只依赖 `yaml` 与标准库），具体运行输出**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：若把 `my_bits.yaml` 里 `mlp.down_proj` 改成只写 `w_bits: 4`（删掉 `a_bits`），加载会怎样？

**答案**：第③层 `_check_complete` 会在 `mlp.down_proj` 节点检测到 `has_w != has_a`，抛 `ValueError: Incomplete bit entry at 'mlp.down_proj'...`。注意它**不会**「悄悄回退到顶层补上 a_bits」——校验发生在加载时，早于任何查询。

**练习 2**：若 `moe.shared` 下既没写 `w_bits/a_bits`，也没写任何叶子，查询 `linear_bits(name="gate_proj", group="moe.shared")` 会返回什么？

**答案**：chain 会收集到 `[moe, shared]`（假设两者都是 dict）；逆序看，`shared` 和 `moe` 都没有完整的 w/a 对，于是全部跳过，最终回退到顶层 `self.w_bits, self.a_bits`。

---

### 4.4 _GroupBits 下标代理与下游消费

#### 4.4.1 概念说明

`linear_bits(name, group)` 的调用形式有点啰嗦：`bp.linear_bits(name="down_proj", group="mlp")`。AMCT 希望下游代码能用更自然的「嵌套字典」写法：

```python
bits = quant_args.bit_policy["mlp"]      # 拿到 mlp 组的「位宽视图」
gate, up, down = bits["gate_proj"], bits["up_proj"], bits["down_proj"]
w = gate.w                                # 权重位宽
a = gate.a                                # 激活位宽
```

这就是 `_GroupBits` 的作用：它是一个**代理对象**，把「先固定 group，再按 name 查」的两步查询，伪装成 `[name]` 下标访问。固定了 group 之后，每次 `[name]` 都自动带上这个 group 去调 `linear_bits`。

#### 4.4.2 核心流程

```text
bit_policy["mlp"]            #  BitPolicy.__getitem__("mlp")  → 返回 _GroupBits(group="mlp")
   ├── _GroupBits["down_proj"]  →  调 linear_bits(name="down_proj", group="mlp") → LayerBits(w,a)
   └── _GroupBits.default       →  调 linear_bits(name=None,      group="mlp") → 组级/顶层回退
```

`BitPolicy.__getitem__` 见 [amct_pytorch/quantization/bit_policy.py:L61-L62](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/bit_policy.py#L61-L62)：它不查表，只是「绑定 group」造一个代理。

#### 4.4.3 源码精读

`_GroupBits` 见 [amct_pytorch/quantization/bit_policy.py:L28-L45](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/bit_policy.py#L28-L45)：

```python
class _GroupBits:
    __slots__ = ("_policy", "_group")

    def __init__(self, policy: "BitPolicy", group: str):
        self._policy = policy
        self._group = group

    def __getitem__(self, name: str) -> LayerBits:
        return LayerBits(*self._policy.linear_bits(name=name, group=self._group))

    @property
    def default(self) -> LayerBits:
        return LayerBits(*self._policy.linear_bits(group=self._group))
```

三个细节：

1. **`__slots__`**：只允许 `_policy` / `_group` 两个属性，省内存（一个模型有几十层、每层都可能拿一次代理）。
2. **`__getitem__` 转发**：把 `[name]` 翻译成 `linear_bits(name=name, group=self._group)`，再用 `LayerBits(*tuple)` 把 `(w,a)` 元组展成命名元组。
3. **`default` 属性**：调 `linear_bits(group=..., name=None)`，即「不指定叶子，只取组级或顶层」。当你只想知道「这一组的默认位宽」而非某个具体层时用它。

**真实消费方：QuantGatedMLP**。看 [amct_pytorch/common/models/llm/common/quant_apply.py:L151-L163](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/quant_apply.py#L151-L163)，这就是位宽策略被「取走」的地方：

```python
bits = quant_args.bit_policy[group]                          # _GroupBits
gate, up, down = bits["gate_proj"], bits["up_proj"], bits["down_proj"]
self.gate_proj = QuantLinear(quant_args, mlp_module.gate_proj, w_bits=gate.w, name="gate_proj")
self.up_proj   = QuantLinear(quant_args, mlp_module.up_proj,   w_bits=up.w,   name="up_proj")
self.down_proj = QuantLinear(quant_args, mlp_module.down_proj, w_bits=down.w, name="down_proj")
self.input_quant  = ActivationQuantizer(quant_args, gate.a)
self.hidden_quant = ActivationQuantizer(quant_args, down.a)
```

对照 4.3.3 的手算表就能看懂：`gate_proj` 的位宽经过 `bits["gate_proj"]` → `linear_bits` 回退 → 拿到 `(w,a)`，`.w` 喂给 `QuantLinear`（权重量化器），`.a` 喂给 `ActivationQuantizer`（激活量化器）。`BitPolicy` 的使命到此结束——后续量化器只用拿到的 `(w_bits, a_bits)`，不再回头查配置。

**旁支：attn-cache 的单值查询**。KV cache 的位宽不是 `(w,a)` 对，而是单值，所以走单独的接口 `cache_bits`，见 [amct_pytorch/quantization/bit_policy.py:L75-L77](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/bit_policy.py#L75-L77)：

```python
def cache_bits(self, key: str) -> int:
    cache = self.cfg.get(_CACHE_GROUP) or {}
    return int(cache.get(key, 16))
```

对应 `example_w4a8.yaml` 里的 `attn-cache: { k: 8, v: 8 }`：`cache_bits("k")` 返回 8，`cache_bits("q")` 因「q 没列」回退到 16。这正是该文件注释 `# q / p not listed -> stay at 16` 的实现。

**旁支：没有 yaml 时的兜底**。若命令行没传 `--bit_config`，args.py 会构造一个空 `BitPolicy()`（全 16）；而在 `quant_apply` 内部还有一个二次保险 `ensure_bit_policy`，见 [amct_pytorch/quantization/bit_policy.py:L139-L156](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/bit_policy.py#L139-L156)：若 `args.bit_policy` 已存在就直接返回，否则用零散的 `--w_bits/--a_bits/--q_bits/...` 参数拼一个带 `attn-cache` 的配置。这保证「无论用户用 yaml 还是用零散参数，下游拿到的都是同一个 `BitPolicy` 接口」。

#### 4.4.4 代码实践：跟踪一条位宽从 yaml 到量化器的链路

1. **实践目标**：把「yaml 一行 → linear_bits 回退 → QuantLinear 的 w_bits」整条链路在脑子里跑通。
2. **操作步骤**（源码阅读型实践，无需运行）：
   - 在 `example_w4a8.yaml` 找到 `mlp.down_proj: { w_bits: 4, a_bits: 16 }`。
   - 打开 [amct_pytorch/common/models/llm/common/quant_apply.py:L151-L163](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/quant_apply.py#L151-L163)，假设 `group="mlp"`。
   - 沿 `bits["down_proj"]` → `_GroupBits.__getitem__` → `linear_bits(name="down_proj", group="mlp")` 走一遍。
3. **需要观察的现象**：确认 `down.w = 4` 会传给 `QuantLinear(..., w_bits=4, ...)`，而 `down.a = 16` 会传给 `ActivationQuantizer`——即 down_proj 是「权重量化到 4-bit、激活保持 fp16」的 W4A16 配置。
4. **预期结果**：能复述「`mlp.down_proj` 那一行 yaml 如何最终变成 `QuantLinear` 的 `w_bits=4` 入参」，并理解 `a_bits=16` 意味着该层激活不量化。

#### 4.4.5 小练习与答案

**练习**：`_GroupBits` 同时提供了 `__getitem__(name)` 和 `default` 属性。对 `example_w4a8.yaml` 的 `moe.shared` 组，`bp["moe.shared"]["gate_proj"]` 和 `bp["moe.shared"].default` 各返回什么？二者何时会不同？

**答案**：`bp["moe.shared"]["gate_proj"]`：chain = `[moe, shared]`（shared 无 gate_proj → break），逆序命中 shared 的 `(8,8)` → **(8, 8)**。`bp["moe.shared"].default`：调 `linear_bits(group="moe.shared", name=None)`，parts 只有 `[moe, shared]`，同样命中 shared 的 `(8,8)` → **(8, 8)**。两者在这里相同。**不同的情况**：当某个叶子有自己单独的覆盖时（例如 `moe.routed.down_proj`），`["down_proj"]` 返回叶子值 `(4,16)`，而 `.default` 仍返回组级/顶层值——因为 `default` 不带 name，永远只取组级或顶层。

---

## 5. 综合实践

把本讲三个核心模块串起来，完成一次「为敏感模型设计位宽策略」的小设计。

**背景**：假设你拿到一个 dense LLM，已知三件事——`o_proj` 对量化很敏感（容易掉点）、`down_proj` 反而很鲁棒（可以压狠）、其余层用 W8A8 即可。你希望整体走 W8A8，但兼顾这两层。

**任务**：

1. 写一份 `sensitive.yaml`，满足：
   - 全局 W8A8；
   - `attn-linear.o_proj` 提升到 W16A16（保护敏感层）；
   - `mlp.down_proj` 压到 W4A8（榨干鲁棒层）；
   - KV cache 的 `v` 量化到 8-bit，`k` 保持 16。
2. 写一段 Python，用 `BitPolicy.from_yaml` 加载，并**断言**以下查询结果（示例代码）：

   ```python
   from amct_pytorch.quantization.bit_policy import BitPolicy
   bp = BitPolicy.from_yaml("sensitive.yaml")
   assert bp.linear_bits("o_proj", "attn-linear") == (16, 16)
   assert bp.linear_bits("down_proj", "mlp") == (4, 8)
   assert bp.linear_bits("gate_proj", "mlp") == (8, 8)   # 回退顶层
   assert bp.cache_bits("v") == 8
   assert bp.cache_bits("k") == 16
   assert bp.has_quant_linear() is True
   assert bp.has_quant_cache() is True
   print("OK")
   ```

3. 故意把 `attn-linear.o_proj` 改成只写 `w_bits: 16`，重新加载，确认第③层校验报错并指向 `attn-linear.o_proj`。

**验收标准**：能解释清楚每条 `assert` 为什么成立（哪一步是叶子命中、哪一步是回退顶层、哪一步走 cache 分支）；能说清第 3 步报错为什么发生在「加载时」而非「查询时」。

> 提示：`has_quant_cache` 的判定见 [amct_pytorch/quantization/bit_policy.py:L79-L81](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/bit_policy.py#L79-L81)，只要 `attn-cache` 里任一值 `< 16` 即为 `True`。运行断言的具体输出**待本地验证**。

## 6. 本讲小结

- `BitPolicy` 是 `--bit_config` yaml 的内存表示，职责是「**查位宽 + 校验配置**」，不做量化本身。
- yaml 结构为「顶层全局 → 分组（attn-linear/mlp/moe/attn-cache）→ 子分组（moe.routed/shared）→ 叶子（真实层名）」；其中三个 `_LINEAR_GROUPS` 用 `(w_bits, a_bits)` 对，`attn-cache` 用单值。
- 加载时有**三层校验**：结构（顶层必须 dict）、取值（叶子必须 int ∈ {4,8,16}，且拒绝 bool/str）、完整性（嵌套节点提到 w/a 之一就必须成对）。缺省位宽永远是 16。
- 核心 `linear_bits` 采用「**从最具体叶子到最粗组逐级回退，返回第一个成对完整节点，否则回退顶层**」——这让「组级默认 + 叶子覆盖」可以自然混用。
- `_GroupBits` 是绑定 group 的下标代理，让下游用 `bp["mlp"]["down_proj"].w` 自然取值；真实消费方是 `QuantGatedMLP`，它把 `(w,a)` 分别喂给 `QuantLinear` 和 `ActivationQuantizer`。
- `ensure_bit_policy` 提供「无 yaml 时用零散 `--w_bits/--q_bits/...` 拼配置」的兜底，保证下游接口统一。

## 7. 下一步学习建议

本讲只讲了「位宽策略怎么表达和查询」，还没讲**位宽拿到之后、量化器如何真正用它**。建议接着读：

- [u5-l3 量化算子挂载 quant_apply](u5-l3-quant-apply.md)：看 `QuantGatedMLP` 如何用本讲拿到的 `gate.w / down.a` 构造 `QuantLinear` 和 `ActivationQuantizer`，把位宽策略落地成具体的量化模块。
- [u7-l1 QuantLinear 与量化器模块](u7-l1-quant-modules.md)：深入 `QuantLinear.forward`，看 `w_bits` 如何决定权重走 4-bit / 8-bit / 16-bit（不量化）三条不同路径。
- 若想了解 `has_quant_linear` / `has_quant_cache` 这两个方法被谁消费，可回到 [u3-l1](u3-l1-cli-args.md) 的 `_validate_eval_mode`——那是它们唯一的调用点，用来阻止「eval_mode=bf16 却配了低比特」的矛盾组合。
