# u6-l4 FP8 补丁、限制与二次开发方向

## 1. 本讲目标

这是本手册正册的最后一讲,做三件「收口」的事:

1. **读懂 FP8 补丁**:理解为什么 BF16 权重热更新在 vLLM 上开箱即用,而 FP8 量化权重必须先给 vLLM 打 [patches/vllm_fp8.patch](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/patches/vllm_fp8.patch#L1-L97) 才能正确更新,并逐段读懂补丁改了什么。
2. **盘点项目边界**:整理 README 明确声明的限制(仅测试 vLLM/SGLang、论文中的完美三阶段流水线未实现)以及散落在各处的隐性约束(XPU 无 P2P、vLLM 版本要求等)。
3. **学会二次开发**:以 `VllmColocateWorkerExtension` 为范本,提炼「为新推理框架接入 checkpoint-engine」的实现清单——这是把本项目用在自有推理引擎上的最短路径。

学完本讲,你应该能独立回答:FP8 热更新坏在哪里?补丁为什么用 `copy_` 而不是重新赋值 `Parameter`?如果我的团队自研了一个推理引擎,要写哪些代码才能接上 checkpoint-engine?

## 2. 前置知识

### 2.1 FP8 量化与 weight scale

FP8 是 8 位浮点格式(常见 E4M3:1 符号位、4 指数位、3 尾数位),可表示的动态范围远小于 BF16。因此 FP8 权重通常按块(blockwise)或按张量(per-tensor)携带一个缩放因子,推理时近似还原:

\[ W_{bf16} \approx W_{fp8} \times s \]

在 vLLM 的 FP8 实现里,这个缩放因子叫 `weight_scale_inv`(反量化 scale)。补丁里出现的 `w13_weight` / `w2_weight` 是 MoE(混合专家)层的两组权重:`w13` 是把 gate 权重 `w1` 与 up 权重 `w3` 拼在一起的 fused 矩阵,`w2` 是 down 权重。热更新把新的 `W_fp8` 与 `s` 都送进引擎后,引擎还需要**重新执行量化后处理**(重排、padding、必要时 requantize),这正是后面 `process_weights_after_loading` 的职责。

### 2.2 `Parameter`、`setattr` 与 `copy_`:指针为什么重要

`torch.nn.Parameter` 就是带 `requires_grad` 标志的 `Tensor`。给 layer 换权重有两种根本不同的写法:

- **替换对象**:`layer.weight = Parameter(new_tensor)` —— 属性名指向一个**全新的对象**,底层分配**新的显存**,数据指针(`data_ptr()`)变了。
- **原地写**:`layer.weight.copy_(new_tensor)` —— 对象不变、显存地址不变,只是把数值刷进去。

如果引擎里还有别的代码**持有旧对象的引用**(比如把 `layer.weight.data_ptr()` 记在了别处),这两种写法的后果完全不同:前者让那些引用永远停留在旧地址,后者让所有引用同步看到新值。

### 2.3 CUDA Graph 与指针固化

vLLM V1 的 decode 阶段大量使用 CUDA Graph:把整步前向的 kernel 序列**一次性录制**成图,之后每步直接重放,省去 CPU 侧 launch 开销。录制的 kernel 参数里**固化了当时权重张量的数据指针**。这带来一条铁律:

> 权重热更新如果替换了 `Parameter` 对象(指针变了),已录制的 CUDA Graph 仍然读旧地址——更新「不生效」,甚至读到已释放的显存。原地 `copy_` 则不需要重新捕获图。

补丁作者的注释直接点明了这个动机:"directly copy it from weight to keep pointer unchanged in CUDA Graph"。

### 2.4 怎么读一个 unified diff 补丁

`vllm_fp8.patch` 是标准 git format-patch 格式,读法:

- `--- a/... +++ b/...`:被修改的文件(相对 vLLM 仓库根)。
- `@@ -387,10 +406,9 @@ ...`:hunk 头。`-387,10` 表示旧文件从第 387 行起 10 行,`+406,9` 表示新文件从第 406 行起 9 行;行尾的 `class Fp8LinearMethod` 是 git 自动摘取的「最近一次类/函数定义」上下文,帮你定位 hunk 所在的类。
- 空格开头 = 上下文行(不动),`-` 开头 = 删除,`+` 开头 = 新增。
- 应用方式:`cd <vLLM 源码目录> && git apply patches/vllm_fp8.patch`(或 `patch -p1 < ...`)。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [patches/vllm_fp8.patch](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/patches/vllm_fp8.patch#L1-L11) | 本讲主角:对 vLLM 的 FP8 量化模块(fp8.py、kv_cache.py)的两文件补丁 |
| [README.md](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/README.md#L155-L161) | FP8 小节(为什么要补丁、测试范围、上游 PR)、Benchmark、Limitations 与 Future Work |
| [checkpoint_engine/worker.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L54-L131) | 引擎无关的 `update_weights_from_ipc` 状态机(两个注入点)与 `VllmColocateWorkerExtension`(扩展范本) |
| [checkpoint_engine/ps.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L51-L59) | 旁及:设备 UUID 的 PS 侧定义(扩展必须与之逐字符对齐) |
| [tests/test_update.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_update.py#L107-L112) | 旁及:用注入 `run`/`post_hook` 的方式复用真实状态机——二次开发的现成参照 |

## 4. 核心概念与源码讲解

### 4.1 vllm_fp8.patch:FP8 热更新的兼容性问题

#### 4.1.1 概念说明

checkpoint-engine 的热更新路径是「绕过 vLLM 启动流程」的:冷启动用 `--load-format dummy` 占位,u3-l4 学过的广播流水线把新权重经 CUDA IPC 直送 worker,worker 调 `model.load_weights(...)` 装载,再在「第二个 None」时执行 `process_weights_after_loading` 做量化后处理(u4-l1/u4-l2)。

BF16 模型走这条链是平顺的:`load_weights` 把数值 copy 进已存在的 `Parameter`,`process_weights_after_loading` 对 BF16 基本是 no-op。FP8 模型则不同——vLLM 的 FP8 后处理代码**假设自己只在模型冷启动时运行一次**,因此写法非常随意:直接用全新的 `Parameter` 对象替换 layer 属性、直接访问可能不存在的 scale 属性。这些假设在「反复热更新」的场景下全部踩雷,于是需要一个补丁把 FP8 后处理改造成**可重入(reentrant)、指针稳定**的形态。

注意本模块的边界:补丁**不在本仓库生效**,它修改的是 vLLM 源码;README 明确说它只在 DeepSeek-V3.1 和 Kimi-K2 上测试过。

#### 4.1.2 核心流程

先把「问题触发链」画出来:

```text
PS 广播第 g 个桶
  └─ worker 收到张量清单 (list)
       └─ run 注入点 = VllmColocateWorkerExtension._load_weights
            └─ vLLM model.load_weights([(name, tensor), ...])   ← FP8 权重数值进入 layer
… 全部桶完成 …
PS 发来第二个 None
  └─ post_hook 注入点 = _post_hook
       └─ vLLM process_weights_after_loading(model, ...)
            └─ 逐层调用 Fp8LinearMethod / Fp8MoEMethod / BaseKVCacheMethod
                 的 process_weights_after_loading                ← 补丁要修的就是这里
```

对照 BF16 与 FP8 在同一链路上的差异:

| 环节 | BF16 | FP8 |
| --- | --- | --- |
| `load_weights` | 数值进 `Parameter` | 数值进 `Parameter`(原始或 blockwise 量化形态) |
| 后处理 | 基本无事可做 | requantize / padding / 重排,并**重建** weight 与 scale |
| 原生实现的可重入性 | 无所谓(本来不改对象) | 差:重建 = 新 `Parameter` 对象 + 新指针 |
| 结果 | 开箱即用 | CUDA Graph 读旧指针、属性丢失 → 必须打补丁 |

#### 4.1.3 源码精读

README 的 FP8 小节直说了问题与边界:

- [README.md:L155-L161](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/README.md#L155-L161) —— 声明「FP8 量化目前在 vLLM 中无法原生支持权重更新」,给出补丁路径、测试范围(仅 DeepSeek-V3.1 与 Kimi-K2,其他模型可能有兼容问题),以及指向 vLLM 上游 [PR #24488](https://github.com/vllm-project/vllm/pull/24488)。

补丁本身的元信息:

- [patches/vllm_fp8.patch:L1-L11](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/patches/vllm_fp8.patch#L1-L11) —— 补丁标题即问题概括:「use _wrap_parameter_or_copy instead of using Parameter and add missing scale attributes」(用 `_wrap_parameter_or_copy` 取代直接构造 `Parameter`,并补上缺失的 scale 属性)。diffstat 显示只改 2 个文件、+36/-11 行,是个小而准的修复。

Benchmark 表佐证补丁的效果——FP8 模型的更新耗时与 BF16 同量级,说明打补丁后 FP8 并没有成为瓶颈:

- [README.md:L51-L54](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/README.md#L51-L54) —— DeepSeek-V3.1 (FP8) 与 Kimi-K2-Instruct (FP8) 在 16×H20 与 256×H20 上的 Broadcast/P2P 成绩(Kimi-K2 1T 参数 256×H20 上 Broadcast 16.04s)。

#### 4.1.4 代码实践

**实践:解剖补丁的全貌(纯 CPU,只读操作)**

1. **实践目标**:不打开 vLLM 仓库,仅凭补丁文件本身回答「它改了哪些文件的哪些类、各加删多少行」。
2. **操作步骤**:
   - 在项目根目录执行 `git apply --stat patches/vllm_fp8.patch`(`--stat` 只打印统计,不会修改任何文件);
   - 再执行 `git apply --numstat patches/vllm_fp8.patch` 获得机器可读的增删行数;
   - 打开补丁文件,把每个 `@@ ... @@` hunk 头抄成一张表:hunk 所在类、删除了几行、新增了几行。
3. **需要观察的现象**:`--stat` 应输出两个文件路径(`.../quantization/fp8.py`、`.../quantization/kv_cache.py`)及各自的增删统计,总计 2 files changed, 36 insertions(+), 11 deletions(-)。
4. **预期结果**:你得到一张 4 行的 hunk 清单(fp8.py 三个 hunk + kv_cache.py 一个 hunk),并能说出每个 hunk 所属的类名——这正是下一模块的精读地图。
5. 本讲义生成环境未执行该命令,输出格式**待本地验证**(`git apply --stat` 的行为是 git 文档保证的,预期可靠)。

#### 4.1.5 小练习与答案

**练习 1**:为什么 README 说补丁「只在 DeepSeek-V3.1 和 Kimi-K2 上测试过,其他模型可能有兼容问题」?补丁的哪一部分最可能引发模型间差异?

**答案**:补丁修改的是 vLLM `Fp8LinearMethod`、`Fp8MoEMethod`、`BaseKVCacheMethod` 三个量化方法类的后处理逻辑。不同模型的量化配置不同(per-tensor vs blockwise scale、是否带 KV cache scale、MoE 结构差异),走到的代码分支与依赖的属性集合不同;补丁里 `hasattr(layer, "q_scale")` 这类「缺失补建」逻辑只覆盖了被测模型的属性组合,其他模型的量化方法(如别的 scale 名字)未必被照顾到。

**练习 2**:如果 vLLM 上游合并了 PR #24488,使用 checkpoint-engine 的 FP8 用户还需要做什么?

**答案**:不需要再手动打补丁——补丁的修改会随 vLLM 正式发布生效。但要注意版本矩阵:只有包含该 PR 的 vLLM 版本才内置修复,旧版本(README 推荐的 v0.10.2)仍需 `git apply patches/vllm_fp8.patch`。

### 4.2 补丁两大修改点:指针稳定性与缺失 scale 补建

#### 4.2.1 概念说明

补丁的两个文件对应两类问题:

1. **fp8.py:替换 `Parameter` 破坏指针稳定**(三个 hunk,同一主题)。原生代码在后处理里写 `layer.weight = Parameter(weight, requires_grad=False)`,冷启动时无妨;热更新时每次执行都会:换对象、分配新显存、丢弃挂在旧对象上的 `weight_loader` 属性。修复思路是新增 `_wrap_parameter_or_copy`:属性已是 `Parameter` 就 `copy_` 原地刷值,否则才创建新 `Parameter`——「首次冷启动走创建、后续热更新走原地写」的统一分岔。
2. **kv_cache.py:后处理访问可能不存在的属性**(一个 hunk)。补丁注释写明:"update weights may miss these attributes, we create it if not present"——热更新路径直接进入 `process_weights_after_loading` 时,某些 layer 可能还没建立 `q_scale` 等属性,原生代码直接访问会 `AttributeError`;修复是在开头检测缺失并调用 `create_weights` 补建。(属性为何缺失补丁未展开,基于注释的合理解释是:冷启动时这些属性由模型构建期的 `create_weights` 建立,而热更新绕过了部分构建步骤。)

#### 4.2.2 核心流程

`_wrap_parameter_or_copy(layer, name, weight)` 的分岔逻辑:

```text
读取 layer.<name>
├─ 已是 Parameter(模型已初始化,热更新路径)
│    └─ layer_weight.copy_(weight)      # 对象不变、指针不变 → CUDA Graph 安全
└─ 不是 Parameter(首次加载/冷启动路径)
     └─ param = Parameter(weight, requires_grad=False)
        ├─ 旧属性带 weight_loader? → 拷到 param 上      # 保住后续 load_weights 的钩子
        └─ setattr(layer, name, param)
```

`BaseKVCacheMethod.process_weights_after_loading` 开头的补建逻辑:

```text
if not hasattr(layer, "q_scale"):        # 热更新可能没建这些属性
    assert not hasattr(layer, "k_scale")   # 四个 scale 必须同时缺失
    assert not hasattr(layer, "v_scale")   # (防御式断言:半建状态视为异常)
    assert not hasattr(layer, "prob_scale")
    self.create_weights(layer)             # 按冷启动的方式补建全部属性
```

#### 4.2.3 源码精读

**修改点一:新增辅助函数**(fp8.py 第一个 hunk)

- [patches/vllm_fp8.patch:L21-L38](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/patches/vllm_fp8.patch#L21-L38) —— 新增 `_wrap_parameter_or_copy`,注释写明两条动机:「directly copy it from weight to keep pointer unchanged in CUDA Graph」与「keep the weight_loader attribute to make sure the weight can be loaded correctly in weight update」。

**修改点二:线性层**(fp8.py 第二个 hunk,位于 `Fp8LinearMethod`)

- [patches/vllm_fp8.patch:L43-L56](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/patches/vllm_fp8.patch#L43-L56) —— 把原生代码里对 `layer.weight`、`layer.weight_scale_inv` 的两次 `Parameter(...)` 替换(删除行 L47-L50)改为两次 `_wrap_parameter_or_copy` 调用(新增行 L51-L53)。上下文里可见 `weight = self._maybe_pad_weight(weight)`,说明此处处于 FP8 权重 padding/重排后的落盘阶段。

**修改点三:MoE 层**(fp8.py 第三个 hunk,位于 `Fp8MoEMethod`)

- [patches/vllm_fp8.patch:L57-L77](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/patches/vllm_fp8.patch#L57-L77) —— 同样的替换应用于 MoE 的四组张量:`w13_weight`、`w13_weight_scale_inv`、`w2_weight`、`w2_weight_scale_inv`(删除行 L61-L67 → 新增行 L68-L73)。四组替换说明 FP8 MoE 的权重与 scale 在后处理中会被整体重建,是热更新踩雷的重灾区。

**修改点四:KV cache scale 补建**(kv_cache.py 唯一 hunk,位于 `BaseKVCacheMethod`)

- [patches/vllm_fp8.patch:L82-L95](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/patches/vllm_fp8.patch#L82-L95) —— 在 `process_weights_after_loading` 开头插入缺失检测与 `create_weights(layer)` 补建(L86-L91)。

#### 4.2.4 代码实践

**实践:纯 CPU 复现「指针稳定性」问题(示例代码)**

1. **实践目标**:用 10 行代码亲眼看到「替换 `Parameter`」与「`copy_` 原地写」对**外部持有的引用**造成的差异——这正是补丁要修复的核心。
2. **操作步骤**:把下面的示例代码存为 `pointer_stability_demo.py` 并运行(只需 CPU 版 PyTorch,与 vLLM 无关):

   ```python
   # 示例代码:模拟 CUDA Graph / 下游代码持有旧权重引用的场景
   import torch
   from torch.nn import Parameter

   layer = torch.nn.Linear(4, 4, bias=False)
   external_ref = layer.weight            # 模拟 CUDA Graph 录制时固化的引用
   old_ptr = external_ref.data_ptr()

   # 写法 A:原生 vLLM 的做法 —— 替换对象
   layer.weight = Parameter(torch.ones_like(layer.weight), requires_grad=False)
   print("A: 外部引用看到的值 =", external_ref.flatten()[:2].tolist())
   print("A: 指针变化 =", external_ref.data_ptr() != old_ptr)

   # 写法 B:补丁的做法 —— 原地 copy_
   layer.weight.copy_(torch.full_like(layer.weight, 7.0))
   print("B: 外部引用看到的值 =", external_ref.flatten()[:2].tolist())
   print("B: 指针变化 =", external_ref.data_ptr() != old_ptr)
   ```

3. **需要观察的现象**:写法 A 之后 `external_ref` 仍是旧对象(值不是 1.0,指针已与 `layer.weight` 脱钩);写法 B 之后 `external_ref` 同步变成 7.0 且指针不变。
4. **预期结果**:`A: 外部引用看到的值` 为初始化随机值、`A: 指针变化 = True`;`B: 外部引用看到的值 = [7.0, 7.0]`、`B: 指针变化 = False`。把它映射回真实场景:外部引用就是 CUDA Graph 固化的 kernel 参数,A 等于更新不生效。
5. 本讲义生成环境未安装可运行的 PyTorch,**待本地验证**(结论由 `Parameter` 替换语义与 `copy_` 原地语义保证,预期可靠)。

#### 4.2.5 小练习与答案

**练习 1**:补丁为什么在 `isinstance(layer_weight, Parameter)` 分支里**不做** `weight_loader` 属性的拷贝?

**答案**:`copy_` 分支根本没有创建新对象,`layer_weight` 就是原来的那个对象,挂在它上面的 `weight_loader` 属性天然还在,无需处理。只有走 `setattr` 新建对象时,旧属性才会随旧对象一起被丢弃,所以才要在那里手工把 `weight_loader` 迁移过来。

**练习 2**:kv_cache.py 的补丁为什么用四个 `assert`(断言 k/v/prob_scale 也缺失)而不是分别对每个属性做 `hasattr` 补建?

**答案**:`create_weights(layer)` 是整体建属性的冷启动函数,一次调用会建立全部 scale 属性。若出现「有 `q_scale` 却没有 `k_scale`」的半建状态,说明状态已经异常(不是简单的「没初始化」),此时静默补建可能掩盖真正的错误;断言把这种不一致显式暴露出来。这是防御式编程:只接受「全无」与「全有」两种合法状态。

**练习 3**:热更新场景下,即使指针不变化,`_wrap_parameter_or_copy` 的 `copy_` 分支每次仍要搬运整个权重张量。这与 checkpoint-engine 流水线里哪个阶段的搬运重复了?为什么可以接受?

**答案**:`load_weights` 已经把 IPC buffer 里的新权重写入 `layer.weight` 一次(那是第一层搬运),后处理的 `copy_` 把 padding/requantize 的结果再写回一次(第二层搬运)。可以接受的原因:FP8 的后处理变换(padding、重排、requantize)本来就必须产出新数据,`copy_` 只是把结果落回原地址;相比「替换对象导致 CUDA Graph 全部重捕(或更新失效)」,这点拷贝开销小得多,而 Benchmark 表(README L51-L54)显示 FP8 更新仍与 BF16 同量级。

### 4.3 update_weights_from_ipc:引擎无关的状态机与两个注入点

#### 4.3.1 概念说明

补丁是「vLLM 侧的修复」,而**引擎无关的通用层**才是二次开发的支点。`checkpoint_engine/worker.py` 里的模块级函数 `update_weights_from_ipc`(u4-l1 精读过它的状态机)是全项目唯一面向推理引擎的**通用**入口:它不含任何 vLLM import,所有引擎相关逻辑都被抽成了两个回调参数:

- `run`:每收到一桶张量清单时调用,负责「把 `[(name, tensor), ...]` 装进模型」;
- `post_hook`:收到第二个 `None`、所有桶装完后调用一次,负责「权重后处理」。

FP8 补丁修的就是 `post_hook` 链路下游的 vLLM 代码。理解「补丁问题 → 注入点 → 引擎实现」这三层的对应关系,是本讲承上启下的关键。

#### 4.3.2 核心流程

```text
update_weights_from_ipc(zmq_ctx, zmq_handle, device_id, run, post_hook)
├─ connect REP socket,收 IPC 句柄,attach 出共享 buffer        [引擎无关]
└─ 循环收消息:
   ├─ list   → run(_extract_weights(payload, buffer)) → ACK    [注入点 1:引擎的 load_weights]
   ├─ None#1 → synchronize + 释放 IPC + gc/empty_cache → ACK   [引擎无关]
   ├─ None#2 → post_hook() → synchronize → ACK → 退出          [注入点 2:引擎的量化后处理]
   └─ Exception → raise(由 PS 统一下发的退出指令)
```

两个注入点的引擎侧实现(下一模块的范本):

| 注入点 | 通用层签名 | vLLM 实现 | FP8 相关性 |
| --- | --- | --- | --- |
| `run` | `Callable[[list[tuple[str, Tensor]]], None]` | `model_runner.model.load_weights(...)`(+MTP drafter) | 装入新 FP8 权重与 scale |
| `post_hook` | `Callable[[], None]` | `process_weights_after_loading(model, ...)` | 触发补丁修复的后处理 |

#### 4.3.3 源码精读

- [checkpoint_engine/worker.py:L54-L77](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L54-L77) —— 函数签名把引擎依赖压缩到只有两个 keyword-only 回调(`run`、`post_hook`,L59-L60);前半段只做 ZMQ 连接与 IPC attach,不含任何引擎概念。
- [checkpoint_engine/worker.py:L78-L82](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L78-L82) —— 源码里的状态机注释,四类消息的语义一目了然;注意 `TODO: wrap all messages to an object instead of None and Exception`(L95)——协议本身还有改进空间,这也是潜在贡献点。
- [checkpoint_engine/worker.py:L108-L117](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L108-L117) —— **注入点 1** 的落点:`run(_extract_weights(payload, buffer))`;引擎抛错时不 raise 而是回传 traceback 文本(L113-L117),交由 PS 全局约决(u3-l4 的错误传播链)。
- [checkpoint_engine/worker.py:L87-L93](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L87-L93) —— **注入点 2** 的落点:`released` 为真时再收到 `None` 就执行 `post_hook()` 并 ACK 退出。
- [tests/test_update.py:L107-L112](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_update.py#L107-L112) —— 通用性最硬的证据:测试的 checker 进程没有 vLLM,直接以 `run=lambda weights: check(...)`、`post_hook=lambda: synchronize()` 复用真实状态机。**你的新引擎扩展本质上就是这个模式的工程化。**

#### 4.3.4 代码实践

**实践:源码阅读型——追踪两个注入点的完整调用链**

1. **实践目标**:把「PS 消息 → 通用状态机 → 注入点 → vLLM 具体 API」四层链条手工串一遍,产出一张调用链卡片。
2. **操作步骤**:
   - 在 [checkpoint_engine/worker.py:L204-L223](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L204-L223) 找到两个注入点的 vLLM 实现:`_load_weights`(L204-L212,主模型 + MTP drafter)与 `_post_hook`(L214-L223);
   - 顺藤摸瓜记录每层:PS 的 `_update_per_bucket` 发张量清单 → 通用 `update_weights_from_ipc` L110 调 `run` → `_load_weights` → `self.model_runner.model.load_weights`;
   - 再记第二条链:PS 发第二个 `None` → L90 调 `post_hook` → `_post_hook` → `vllm...process_weights_after_loading` → (打补丁后) `_wrap_parameter_or_copy`;
   - 用 `grep -n "load_weights" checkpoint_engine/worker.py` 验证你的行号。
3. **需要观察的现象**:两条链都终止在 vLLM 侧,且 FP8 补丁恰好位于第二条链的末端——补丁、注入点、消息协议三层如何各司其职。
4. **预期结果**:一张两层调用链卡片,每层标注文件与行号;能口头复述「为什么补丁不放在 checkpoint-engine 仓库里也能生效」(因为它修的是链路末端 vLLM 自己的代码)。
5. 本实践为纯静态阅读,无需运行环境,可直接完成。

#### 4.3.5 小练习与答案

**练习 1**:如果把 FP8 补丁的思路「挪进」checkpoint-engine(比如在 `run` 回调里自己做 requantize),可行吗?有什么代价?

**答案**:技术上可行——`run` 拿到的是原始 `(name, tensor)` 列表,可以在其中插入量化逻辑。但代价是 checkpoint-engine 必须理解每个引擎、每种量化方案的内部表示(scale 的分块方式、padding 规则、`w13` 拼接约定),通用中间件被引擎细节污染;而且引擎的 CUDA Graph 捕获发生在引擎内部,中间件无法保证替换对象后图被正确重建。放在引擎侧(补丁/上游 PR)让「谁拥有权重布局,谁负责可重入更新」,职责更干净。

**练习 2**:`run` 回调每桶执行一次,`post_hook` 整个更新只执行一次。假如某引擎的量化后处理必须在**每个** layer 装载后立即做(不能等全部桶到齐),直接接入会有什么问题?

**答案**:`run` 只拿到当前桶的张量,而某层的 `weight` 与 `weight_scale_inv` 可能被切进**不同的桶**(桶按 owner 与字节区间切分,不按层,u3-l5),分批后处理会拿到不完整的一层。这属于引擎侧约束,需要在引擎的 `load_weights` 内部做缓冲/延迟处理,或与 checkpoint-engine 的 bucket 切分约定对齐——这也是二次开发时要提前想清楚的问题。

### 4.4 worker 扩展接口:为新推理框架设计接入层

#### 4.4.1 概念说明

`VllmColocateWorkerExtension` 是「引擎适配层」的完整范本。它的设计哲学在本讲升华成一句话:**checkpoint-engine 只要求引擎提供两个回调与一条控制通道,其余全部由通用层承担**。README 的 Limitations 说「目前仅测试 vLLM 与 SGLang,其他框架的集成是未来工作」——而 SGLang 的编排脚本在 SGLang 仓库(`sglang.srt.checkpoint_engine.update`),本仓库并不含 SGLang worker 代码;所以要接入**你自己**的推理框架,`VllmColocateWorkerExtension` 就是唯一可直接参照的样板。

#### 4.4.2 核心流程

新框架接入的五个必要件(每条都对应范本中的一段真实代码):

```text
① 控制通道:能从外部触发所有 worker 进程上的同名方法
      (vLLM:/collective_rpc + --worker-extension-cls 注入扩展类)
② 设备 UUID:与 ps.py::_get_physical_gpu_id 逐字符一致的生成规则
      (worker 用它在自己这张卡对应的 ZMQ 地址上 connect)
③ 权重装载入口:load_weights([(name, tensor), ...]) 风格的 API
      (注入为 run;还需考虑 MTP/drafter 等副模型)
④ 权重后处理入口:量化 requant / CUDA Graph 兼容的钩子
      (注入为 post_hook;FP8 场景必须可重入——见 4.2)
⑤ 冷启动占位:跳过磁盘权重加载,等热更新送权重
      (vLLM:--load-format dummy)
```

#### 4.4.3 源码精读

- [checkpoint_engine/worker.py:L134-L148](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L134-L148) —— 类 docstring 明确说明注入机制:本类的方法会被注入 vLLM worker 类,可经 `collective_rpc` API 调用,兼容 v0/V1。即必要件①的官方契约。
- [checkpoint_engine/worker.py:L150-L162](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L150-L162) —— 必要件②:CUDA 用 `current_platform.get_device_uuid`、NPU 用 `NPU-{npu_generate_uuid()}`、XPU 用 `GPU-{torch.xpu...uuid!s}`;L159 的注释直言 "Must match ps.py::_get_physical_gpu_id ... for the ZMQ key to resolve"。PS 侧的定义在 [checkpoint_engine/ps.py:L51-L59](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L51-L59),两侧不一致的下场是查表 `KeyError`(u4-l2 讲过)。
- [checkpoint_engine/worker.py:L168-L231](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L168-L231) —— 扩展的主方法:接收 `zmq_handles: dict[str, str]`(设备 UUID → ZMQ 地址),兜底初始化 device(L197-L202,NPU/XPU 上 vLLM 可能未设 `self.device`),定义 `_load_weights` 与 `_post_hook` 两个闭包,最后把两者注入通用状态机(L225-L231)。
- [checkpoint_engine/worker.py:L204-L212](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L204-L212) —— 必要件③的范本:同一份权重同时喂主模型与 drafter(MTP 投机解码),两个模型按名字各取所需——你的引擎若有副模型,这里就是提醒。
- [checkpoint_engine/worker.py:L214-L223](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L214-L223) —— 必要件④的范本:调用 vLLM 的 `process_weights_after_loading` 补上被热更新绕过的后处理,同样覆盖 drafter。FP8 补丁(4.2)就是为让这一步可重入而生。
- [README.md:L123-L129](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/README.md#L123-L129) —— 必要件⑤:启动 vLLM 时 `--worker-extension-cls checkpoint_engine.worker.VllmColocateWorkerExtension` 与 `--load-format dummy` 的完整命令行。
- [README.md:L206-L210](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/README.md#L206-L210) —— SGLang 的接入入口是 `python -m sglang.srt.checkpoint_engine.update`:注意该脚本属 SGLang 仓库,是「其他框架接入」的又一个已存在参照(控制通道与扩展机制由 SGLang 侧实现)。

#### 4.4.4 代码实践

**实践:为假想引擎写一个 worker 扩展骨架(示例代码,纯 CPU 可写)**

1. **实践目标**:不依赖 vLLM,产出 `MyEngineWorkerExtension` 骨架,把 4.4.2 的五个必要件逐一落成代码位置,并自查与通用状态机的接线。
2. **操作步骤**:

   ```python
   # 示例代码:假想引擎 TinyLLM 的 worker 扩展骨架(仅演示结构,不可运行)
   from checkpoint_engine.worker import update_weights_from_ipc

   class TinyLLMWorkerExtension:
       """必要件①:本类需被引擎以某种 RPC 机制对全体 worker 调用
       (vLLM 用 --worker-extension-cls + /collective_rpc,TinyLLM 需自建等价通道)。"""

       @cached_property
       def _device_uuid(self) -> str:
           # 必要件②:必须与 ps.py::_get_physical_gpu_id 逐字符一致
           return f"GPU-{torch.cuda.get_device_properties(self.device.index).uuid!s}"

       def update_weights_from_ipc(self, zmq_handles: dict[str, str]):
           assert self.device is not None  # 引擎侧保证 device 可得

           def _load_weights(weights):
               self.model.load_weights(weights)          # 必要件③:引擎的装载 API
               if self.drafter is not None:               # 副模型按需补喂
                   self.drafter.load_weights(weights)

           def _post_hook():
               self.model.requantize_inplace()            # 必要件④:必须可重入 + 指针稳定!

           update_weights_from_ipc(
               self._zmq_ctx,
               zmq_handles[self._device_uuid],            # 用 UUID 认领自己的 ZMQ 地址
               device_id=self.device.index,
               run=_load_weights,
               post_hook=_post_hook,
           )
   ```

   写完后对照 [worker.py:L168-L231](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L168-L231) 逐行 diff,标出你的引擎还需要补的件。
3. **需要观察的现象**:你的骨架与范本的结构差异集中在两点——控制通道是否存在(①)、`requantize_inplace` 是否指针稳定(④,即 4.2 的补丁教训)。
4. **预期结果**:一份骨架 + 一张「差距清单」(例如:TinyLLM 没有 collective_rpc → 需自建 ZMQ/HTTP 广播;TinyLLM 的 requantize 会 realloc → 需改成 copy_ 式)。骨架为示例代码,**待本地验证**(可与 5. 综合实践结合,用 test_update.py 的替身法在 CPU 上驱动)。
5. 无法在本讲义生成环境中运行,**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**:`zmq_handles` 是 `dict[str, str]`,每个 worker 收到的是**全量**字典,但抽象 Unix domain socket 仅主机内有效。为什么组首只向本节点引擎发本实例的 P 条地址,而不是全部?(回忆 u6-l2)

**答案**:ZMQ 地址形如 `ipc://@checkpoint-engine-<设备UUID>-<计数器>.sock`(见 [ps.py:L622-L630](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L622-L630)),抽象 UDS 不跨主机;每个 worker 只需也只能 connect 到**同卡** PS 的地址,发多余地址既无效也浪费。所以 u6-l2 中组首(rank%P==0)按实例切片下发,worker 再用自身设备 UUID 认领。

**练习 2**:新引擎没有 MTP/drafter,扩展还需要在 `_load_weights` 里做副模型处理吗?范本里 `getattr(self.model_runner, "drafter", None)` 用 `getattr` 而不是直接访问,这给你什么启示?

**答案**:不是必须,但建议保留 `getattr(..., None)` 式的防御性探测:引擎版本升级可能引入副模型,硬编码直接访问会让扩展在新版本上崩溃;`getattr` 探测让同一份扩展跨引擎版本存活。这是适配层的通用韧性技巧。

**练习 3**:五个必要件中,哪一件 checkpoint-engine 仓库本身**无法**替你实现?为什么?

**答案**:①控制通道。它本质是「外部进程触发引擎内部所有 worker 执行任意方法」的能力,属于引擎的安全边界与架构决策(vLLM 为此专门加了 `/collective_rpc` 端点与 `--worker-extension-cls` 参数);checkpoint-engine 只是控制面上的一个客户端,只能调用而不能替引擎开洞。②③④⑤ 分别由 ps.py/通用状态机/引擎 API/引擎启动参数承担或约定。

### 4.5 项目限制与未来工作

#### 4.5.1 概念说明

收口全手册,把「项目自己承认的」与「散落各处的」限制汇总成一张边界图。清楚边界与清楚能力同样重要:它告诉你哪些场景能直接用、哪些要等上游、哪些适合作为你的贡献点。

#### 4.5.2 核心流程

README 声明的两条未来工作,与已学内容的关系:

| README 声明 | 对应现状(本手册已学) | 未来方向 |
| --- | --- | --- |
| 完美三阶段流水线未实现(论文提出) | u3-l4:当前用「h2d_buffer + gidx%2 双缓冲」做两两重叠;显存不足退化串行(u1-l4) | H2D / broadcast / reload **三阶段全程重叠**;对 H2D 与 broadcast 在 PCIe 上不冲突的架构(如独立 PCIe switch / NVLink-D2D)收益大 |
| 仅测试 vLLM 与 SGLang | u4-l2/u6-l2:vLLM 走 worker extension + collective_rpc;SGLang 编排在 SGLang 仓库 | 更多推理框架接入(即 4.4 的清单工程化) |

#### 4.5.3 源码精读

- [README.md:L227-L230](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/README.md#L227-L230) —— Limitations and Future Work 原文:两条声明,一条关于框架覆盖面,一条关于完美三阶段流水线(并注明其适用架构:PCIe 上 H2D 与 broadcast 不冲突的场景)。
- [README.md:L37](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/README.md#L37) —— 「流水线天然需要更多显存,不足时回退串行」:这是当前(非完美)流水线的代价与退路,u3-l4 的 `free/3 → free/2` 探测就是它的实现。
- [README.md:L80](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/README.md#L80) —— 隐性限制一:XPU 仅支持 broadcast,Mooncake 无 Level Zero 后端故无 P2P(u5-l1 的能力开关)。
- [README.md:L102](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/README.md#L102) —— 隐性限制二:vLLM 必须包含 `/collective_rpc` 提交,推荐 v0.10.2;Benchmark 用 v0.10.2rc1([README.md:L56](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/README.md#L56))。
- [README.md:L159](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/README.md#L159) —— 隐性限制三:FP8 补丁只测过两个模型。
- [checkpoint_engine/worker.py:L95](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L95) —— 协议层的 TODO(None/Exception 消息应包装成对象):一个虽小但真实存在的贡献点。

#### 4.5.4 代码实践

**实践:源码阅读型——给「当前流水线」与「完美流水线」画重叠关系图**

1. **实践目标**:用自己的手(纸笔即可)画出 `_update_per_bucket` 四拍循环中,同一时刻哪些操作在并行、哪些共享资源会冲突,从而理解 README 那句「H2D 与 broadcast 不冲突 in PCIE 的架构」。
2. **操作步骤**:
   - 重读 [checkpoint_engine/ps.py:L751-L759](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L751-L759) 起的 `_update_per_bucket`,标出四拍:预取 H2D → 装填半区+广播 → 等 ACK → 发清单;
   - 对每个 gidx,把「第 g 桶的广播」与「第 g+1 桶的 H2D 预取」「worker 装载第 g-1 桶(reload)」画在同一时间轴上;
   - 标注资源:PCIe(H2D 与 broadcast 都要过)、SM/拷贝引擎、显存(3×bucket)。
3. **需要观察的现象**:当前设计中同一 rank 的 H2D 与 broadcast 在时间上交叠但**竞争同一 PCIe 带宽**;「完美三阶段」要做的正是让调度器在两者真冲突时错峰、在不冲突架构上全速重叠。
4. **预期结果**:一张标注了重叠区与冲突资源的时间轴图,并配一句话:为什么 NVLink 侧 broadcast + 独立 PCIe H2D 的机器能从完美流水线获益。
5. 纯静态阅读 + 画图,无需运行环境,可直接完成。

#### 4.5.5 小练习与答案

**练习 1**:「完美三阶段流水线」未实现,但当前实现已经有双缓冲重叠。两者差别到底在哪?

**答案**:当前实现(u3-l4)做到的是**相邻桶之间**的两两重叠:广播第 g 桶时,worker 在装载第 g-1 桶、H2D 在预取第 g+1 桶,且以 3×bucket_size 显存与 ACK 背压为代价,冲突时仍要在同一 PCIe 上分时。论文的完美三阶段是**全局调度最优**:H2D、broadcast、reload 三条流水线全程满载重叠,不因显存预算或 PCIe 竞争降级。前者是工程可用的近似,后者是理想目标。

**练习 2**:你想给项目贡献「支持某个新推理框架」。按照项目现状,你的 PR 需要动哪些仓库?

**答案**:通常两个:本仓库(若需要新的 worker 扩展或新的编排脚本,参照 `VllmColocateWorkerExtension` 与 `examples/update.py`)与目标引擎仓库(需要它提供控制通道与权重装载/后处理 API,参照 vLLM 的 `/collective_rpc` PR 与 FP8 补丁上游化路径)。若引擎侧已具备等价 API,则只改本仓库即可——这正是 4.4 清单要你先盘点引擎能力的原因。

**练习 3**:README 把 NUMA 绑定([README.md:L62](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/README.md#L62))写进 Benchmark 注意事项,这与本讲哪个主题呼应?

**答案**:与流水线性能呼应:H2D 速度是三阶段流水线的第一级,锁页内存所在 CPU 的 NUMA 节点与 GPU 是否亲和直接影响 H2D 带宽;不绑 NUMA 时的跨 socket 访问会让 H2D 成为瓶颈,流水线再完美也快不了。它提醒读者:权重更新的性能是「系统问题」,不全是算法问题。

## 5. 综合实践

**综合任务:为假想引擎 TinyLLM 完成一次「接入评估 + 补丁需求分析」**(贯穿 4.1–4.5,除标注外纯 CPU 可完成):

1. **补丁分析报告(纯阅读)**:阅读 [patches/vllm_fp8.patch](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/patches/vllm_fp8.patch#L1-L97),产出:四个 hunk 的位置表、`_wrap_parameter_or_copy` 的伪代码、以及一段 200 字的「为什么 CUDA Graph 逼迫我们保指针」说明。
2. **指针稳定性实验(纯 CPU)**:运行 4.2.4 的示例代码,把输出贴进报告,并回答:若 TinyLLM 的 requantize 会 realloc,热更新第 2 次起会发生什么?(预期:持有旧指针的图/缓存读到旧值或悬空地址。)**待本地验证**。
3. **接入差距清单(纯阅读)**:对照 4.4.2 的五个必要件,给 TinyLLM 逐条打分(具备/需开发/不可行),明确哪些改动落在本仓库、哪些落在引擎仓库(参考练习 4.4.5-2 的双仓库结论)。
4. **扩展骨架(示例代码)**:完成 4.4.4 的 `TinyLLMWorkerExtension`,并在 `_post_hook` 中显式写注释承诺「指针稳定 + 可重入」——把 FP8 补丁的教训固化成你自己代码的注释契约。
5. **(可选,需环境)CPU 冒烟**:模仿 [tests/test_update.py:L107-L112](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_update.py#L107-L112) 的 checker 模式,用 `run=打印名字`、`post_hook=打印 done` 的替身驱动 `update_weights_from_ipc`,验证骨架接线正确。**待本地验证**。

交付物:一份 Markdown 报告(补丁分析 + 实验输出 + 差距清单 + 骨架代码)。这份报告就是你向团队或开源社区提出「接入 PR」的技术依据。

## 6. 本讲小结

- FP8 热更新的问题不在 checkpoint-engine,而在 vLLM 的量化后处理**假设自己只跑一次**:`Parameter` 对象替换改变数据指针,破坏已录制的 CUDA Graph,还丢失 `weight_loader` 属性;`process_weights_after_loading` 还可能访问未建立的 scale 属性。
- [patches/vllm_fp8.patch](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/patches/vllm_fp8.patch#L21-L38) 的两个抓手:已是 `Parameter` 就 `copy_` 保指针(首次才新建并迁移 `weight_loader`);KV cache scale 缺失时用 `create_weights` 整体补建并以断言拒绝半建状态。补丁只测过 DeepSeek-V3.1 与 Kimi-K2,上游化为 vLLM PR #24488。
- `update_weights_from_ipc` 是引擎无关的通用状态机,引擎依赖被压缩为 `run`/`post_hook` 两个注入点;FP8 补丁正落在 `post_hook` 链路的末端——「中间件管协议,引擎管权重布局」的职责分层是本项目可扩展的根基。
- 新框架接入清单五件套:控制通道、与 ps.py 逐字符一致的设备 UUID、`load_weights` 式装载入口、指针稳定的后处理钩子、冷启动占位;`VllmColocateWorkerExtension` 是唯一范本,`tests/test_update.py` 的 checker 是最小验证法。
- 项目边界:论文的完美三阶段流水线未实现(对 H2D 与 broadcast 不共享 PCIe 带宽的架构有价值);框架覆盖面目前是 vLLM + SGLang(SGLang 编排在其自家仓库);XPU 无 P2P;vLLM 需含 `/collective_rpc`。

## 7. 下一步学习建议

本手册 24 讲到此完结,后续建议按三条线深入:

1. **上游跟踪线**:关注 vLLM PR #24488 的评审结论——若合入,补丁即可退役;同时观察 vLLM `collective_rpc` 与 worker extension 机制的演进,它们决定 4.4 清单中「必要件①」的形态。
2. **动手贡献线**:从 [checkpoint_engine/worker.py:L95](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L95) 的 TODO(把 None/Exception 消息包装成对象)或一个新的引擎扩展入手;前者小而完整,适合作为首个 PR——记得按 u6-l1 的测试范式补 CPU 单元测试。
3. **论文对照线**:阅读 README 引用的 [Kimi-K2 Technical Report](https://arxiv.org/abs/2507.20534) 中关于三阶段流水线的章节,再回到 `_update_per_bucket`(ps.py)对照「论文理想调度」与「工程实现」的差距——这正是 README 声明的头号未来工作,也是最值得社区协作的方向。
