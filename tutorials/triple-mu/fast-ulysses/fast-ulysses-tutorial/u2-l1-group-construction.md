# u2-l1 UlyssesGroup 构造与 NVLink 检查

## 1. 本讲目标

上一单元我们跑通了安装和第一次调用,本讲把镜头对准一切调用的起点——`UlyssesGroup` 的构造。读完本讲,你应该能够:

1. 说出三个构造参数 `process_group` / `device` / `require_nvlink` 各自的语义、默认值,以及它们为什么这样设计。
2. 解释**构造为什么是集合操作**(collective):NVLink 检查里的 `all_gather`、对称内存 bootstrap 注册、以及随后第一次窗口分配的 rendezvous,都要求所有成员 rank 一起到场。
3. 描述 NVLink 检查的完整链路:Python 侧收集设备号 → C++ 侧用 NVML 探测拓扑 → 拓扑不对时拒绝;以及它的**失败开放**(fail-open)语义——NVML 答不上来时放行,而不是误拒。
4. 理解**任意子组**都能用:rank / world_size 是组内编号,对称内存 rendezvous 按子组名字寻址,两个 rank 共享一块 GPU 时检查自动跳过。

## 2. 前置知识

- **进程组(process group)与 SPMD**:torchrun 启动的每个进程叫一个 rank,`dist.init_process_group("nccl")` 之后,所有 rank 默认组成 `WORLD` 组。SPMD 指每个 rank 跑同一份程序、发出同样的调用序列——这是 u1-l2 建立的契约。`dist.new_group(ranks=[...])` 可以在 `WORLD` 里再切出一个子组。
- **集合操作(collective)**:需要组内所有 rank 一起进入、缺一个就全员卡住的操作,比如 `all_gather`、`barrier`。本讲会反复遇到「某个步骤为什么必须是集合的」这个问题。
- **对称内存(symmetric memory)**:torch 提供的一种分配方式,组内每个 rank 各拿一块同尺寸缓冲,并且**互相知道对方的地址**,于是 rank A 可以直接写 rank B 的内存(通过 NVLink)。这是 fast-ulysses 传输的物理基础,细节在 u4 展开;本讲只需要知道「分配对称内存需要一次组内地址交换,叫 rendezvous」。
- **NVML**:NVIDIA Management Library,显卡管理和监控的用户态库(`libnvidia-ml.so.1`),`nvidia-smi` 背后就是它。它是唯一能查到 **NVLink 链路类型**的接口。CUDA 自己的 `cudaDeviceCanAccessPeer` 只能告诉你 P2P 可用与否,PCIe 上它也返回可用,区分不了「快得值得用」和「慢得不如不用」。
- **torchbind 自定义类**:用 `TORCH_LIBRARY(...).class_<...>(...)` 注册、在 Python 侧通过 `torch.classes.命名空间.类名` 访问的 C++ 对象。`UlyssesGroup` 的全部有状态部分(窗口、plan 缓存、暂存池)都住在 C++ 对象里,Python 类只是个壳。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [python/fast_ulysses/group.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py) | Python 门面。`UlyssesGroup.__init__`(构造参数解析、NVLink 检查、对称内存注册)、`_require_nvlink`、`destroy`。 |
| [src/nvlink.cc](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/nvlink.cc) | NVML 探测的全部实现:`dlopen` 加载 NVML、BDF 匹配设备、直连/NVSwitch 两种拓扑、`check_nvlink` 的失败开放。 |
| [include/fast_ulysses/nvlink.hpp](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/nvlink.hpp) | nvlink 模块的契约注释:为什么用 NVML 而不是 `cudaDeviceCanAccessPeer`、`nullopt` 的含义。 |
| [src/group.cc](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc) | C++ 侧 `UlyssesGroup` 构造(`world_size ∈ [1,8]` 检查)与 `allocate()`(对称内存分配 + rendezvous,本讲只看轮廓,u4 精读)。 |
| [src/bindings.cc](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc) | torchbind 类注册与 `check_nvlink` / `nvlink_matrix` 的 pybind 绑定。 |
| [docs/api.md](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md) | 集合契约与构造函数的官方文档,本讲大量论断的出处。 |
| [test/distributed/subgroup.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/subgroup.py) | 子组场景的真实测试,综合实践照着它写。 |

## 4. 核心概念与源码讲解

### 4.1 构造参数:process_group / device / require_nvlink

#### 4.1.1 概念说明

`UlyssesGroup` 的构造函数只有三个参数,但每个都对应一个真实的设计决策:

| 参数 | 默认值 | 语义 | 设计动机 |
| --- | --- | --- | --- |
| `process_group` | `None` → `dist.group.WORLD` | 这组集合通信跑在哪个进程组上 | 支持 tp × sp 二维并行:sp 维度只是 `WORLD` 的一个切片,传子组即可,对子组包含哪些 rank 没有任何限制 |
| `device` | `None` → 当前设备 | 本 rank 用的 CUDA 设备,接受 `torch.device` / 字符串 / 整数 | torchrun 场景里每个进程按 `LOCAL_RANK` 绑好自己的卡,「当前设备」就是正确答案,所以默认值够用 |
| `require_nvlink` | `True`(仅关键字参数) | 组内 GPU 不是全互 NVLink 时拒绝构造 | 传输直接写对端内存,走 PCIe 反而比 `torch.distributed` 还慢——错误拓扑是「正确但慢」,早失败比默默慢好 |

注意 `require_nvlink` 在签名里位于 `*` 之后,是**只能关键字传参**的,这是对「它不常用、别误传」的提示。

#### 4.1.2 核心流程

`__init__` 按严格顺序做八件事,顺序本身就是设计:

```text
1. self._destroyed = True     # 先立"墓碑":任何一步抛异常,__del__ 都能安全运行
2. 解析 process_group         # None → dist.group.WORLD;取组内 rank / world_size
3. 归一化 device              # str/int/None → torch.device("cuda", index)
                              # 非 CUDA → ValueError;无 index → 用当前设备
4. torch.cuda.set_device      # 把进程的当前设备钉在这块卡上
5. require_nvlink=True 时     # _require_nvlink():可能 raise(见 4.3)
6. enable_symm_mem_for_group  # 对称内存 bootstrap 注册(见 4.2)
7. 创建 C++ torchbind 句柄    # 触发 C++ 侧 world_size ∈ [1,8] 检查
8. 创建高优先级 comm 流       # 异步调用专用;最后才把 _destroyed 置 False
```

两个值得停下来想的顺序问题:

- **墓碑先行**:如果第 5 步的 NVLink 检查 raise 了,Python 对象仍然会被创建(只是 `__init__` 没跑完),`__del__` 仍会执行。`_destroyed = True` 放在最前面,保证这个半成品对象在析构时什么都不释放——此时也没有任何东西可释放。
- **NVLink 检查先于对称内存注册**:被拒绝的构造不会在 torch 的对称内存 bootstrap 里留下任何状态,失败是干净的。

#### 4.1.3 源码精读

构造签名与三个参数的文档:[python/fast_ulysses/group.py:L43-L57](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L43-L57)。docstring 里对 `require_nvlink` 的措辞值得记住:"`False` is for measuring that case, not for running in it"——关掉检查的唯一正当理由是测量退化,不是在 PCIe 机器上跑生产。

墓碑、进程组与设备归一化:[python/fast_ulysses/group.py:L58-L75](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L58-L75)。设备归一化的注释解释了历史上踩过的坑:裸字符串 `"cuda:0"` 没有 `.index` 属性,`torch.device("cuda")` 的 `.index` 是 `None`,两者都会在几行之后炸出一个跟设备毫无关系的错误,所以这里统一收敛成带 index 的 `torch.device`。`rank` 和 `world_size` 用 `dist.get_rank(pg)` / `dist.get_world_size(pg)` 取——**是组内编号,不是全局编号**,这一点在 4.4 会变成主题。

对称内存注册与 C++ 句柄创建:[python/fast_ulysses/group.py:L80-L89](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L80-L89)。注册用的 key 是 `pg.group_name`——进程组的**名字**字符串,后面所有 rendezvous 都用它寻址。C++ 句柄就是 `torch.classes.fast_ulysses.UlyssesGroup(...)`,它的注册在 [src/bindings.cc:L680-L681](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L680-L681),构造函数本体在 [src/group.cc:L145-L155](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L145-L155):只做两个 `TORCH_CHECK`——`world_size ∈ [1, 8]`(fast-ulysses 是单机库)和 `rank` 在范围内。docs/api.md 对应的一句话是「`world_size` outside `[1, 8]` is refused by the constructor, so no call reaches it」([docs/api.md:L103](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md#L103))。

最后是 comm 流:[python/fast_ulysses/group.py:L91-L96](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L91-L96)。取 `priority_range()` 的最大优先级(数值最小)建一条新流,**只有异步调用用它**;同步调用留在调用者自己的流上,因为绕道 comm 流要多付两次事件同步。注释还预告了一个 u7 的话题:这两条流之间没有顺序关系,所以 C++ 侧给它们**分开的窗口**。

#### 4.1.4 代码实践

**实践目标**:验证三个参数的归一化行为与失败路径,不依赖多卡。

**操作步骤**(示例代码,非项目文件):

```python
# u2_l1_params.py —— 运行: python u2_l1_params.py(需要已 init 的进程组,见步骤 3)
import os
import torch
import torch.distributed as dist
from fast_ulysses import UlyssesGroup

dist.init_process_group("nccl")            # 构造的前提:进程组已初始化
torch.cuda.set_device(int(os.environ.get("LOCAL_RANK", 0)))

g1 = UlyssesGroup()                        # device=None → 当前设备
g2 = UlyssesGroup(device="cuda")           # 字符串,无 index → 仍解析到当前设备
g3 = UlyssesGroup(device=torch.cuda.current_device())  # 整数索引
assert g1.device == g2.device == g3.device
for g in (g3, g2, g1):
    g.destroy()

try:
    UlyssesGroup(device="cpu")
except ValueError as e:
    print("caught:", e)                    # 预期: device must be a CUDA device, got cpu
```

**需要观察的现象**:
1. 三种 `device` 写法解析到同一个 `torch.device`。
2. `device="cpu"` 抛 `ValueError`,消息明确点名设备。

**预期结果**:归一化逻辑([group.py:L69-L74](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L69-L74))保证 1;`ValueError` 分支([group.py:L70-L71](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L70-L71))保证 2。实际运行输出待本地验证(本讲义写作环境无 GPU)。

#### 4.1.5 小练习与答案

**练习 1**:为什么 `_destroyed = True` 要放在 `__init__` 的第一行,而不是在结尾和 `_destroyed = False` 配对出现的地方顺便处理?

**答案**:构造中途可能 raise(NVLink 拒绝、非 CUDA 设备)。Python 对一个 `__init__` 抛异常的对象仍会执行 `__del__`;若 `_destroyed` 属性不存在,`__del__` 会抛 `AttributeError` 并掩盖原异常。第一行先声明「无资源可释放」,让任何失败路径的析构都是安全的([group.py:L58-L61](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L58-L61) 的注释原文:"a refused construction ... still leaves an object for `__del__` to run on, with nothing to release")。

**练习 2**:`world_size = 9` 的组会在哪里、以什么形式失败?

**答案**:在 C++ 构造函数里,`TORCH_CHECK(world_size_ >= 1 && world_size_ <= 8, ...)`([src/group.cc:L151-L153](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L151-L153))。`TORCH_CHECK` 抛 `c10::Error`,到 Python 侧是 `RuntimeError`,消息为 "world_size must be in [1, 8] -- fast-ulysses is single-node -- got 9"。因为构造参数 `rank` / `world_size` 是 Python 侧从 `pg` 算好再传进去的([group.py:L87-L89](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L87-L89)),这个检查实际卡住的是「传了一个 9 rank 子组」的场景。

**练习 3**:`require_nvlink` 为什么做成仅关键字参数?

**答案**:它是个不常用的开关,且语义(关掉一个安全检查)容易被误用。放在 `*` 之后强制调用方写出 `require_nvlink=False` 这个显式动作,避免位置参数误传,也让代码审查时这行一眼可见。

### 4.2 对称内存注册:构造为何是集合操作

#### 4.2.1 概念说明

docs/api.md 的集合契约开宗明义:**"Construction, `empty_output()` and `destroy()` are collective"**([docs/api.md:L6-L20](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md#L6-L20))。违反集合契约的后果不是报错而是**挂起**——某个 rank 在集合点等一个永远不来的伙伴。

但「构造是集合的」这句话要拆开看,构造函数里其实有三种不同性质的步骤:

1. **真集合通信**:`_require_nvlink` 里的 `dist.all_gather`(见 4.3)——这是一次真正的组内通信,构造路径上唯一的分布式原语。
2. **本地但必须人人执行的注册**:`symm_mem.enable_symm_mem_for_group(pg.group_name)`。它是进程本地的、幂等的注册,但任何一个 rank 漏掉它,这个 rank 后续的对称内存 rendezvous 就找不到组织。
3. **纯本地构造**:C++ 句柄创建、comm 流创建——不通信,但它们的状态(rank、world_size、device)必须全组一致,而这份一致性由「同一份 SPMD 程序」保证。

此外要澄清一个容易误解的点:**构造本身并不分配对称内存窗口**。窗口是惰性的,第一次需要它的调用才分配([group.py:L38-L40](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L38-L40) 的类 docstring:"Windows are allocated on the first call that needs one and cached")。构造的「集合性」是在为那第一次分配**铺路**:每个 rank 都注册了,rendezvous 才能凑齐。

#### 4.2.2 核心流程

```text
构造(每个 rank 各自执行,但输入相同)
  ├─ all_gather 收集全组设备号          ← 真集合通信
  ├─ enable_symm_mem_for_group(name)    ← 本地注册,幂等,人人必须做
  └─ 创建 C++ 句柄 + comm 流            ← 本地

第一次 all_to_all_4d 调用(更严格的集合点)
  └─ allocate():
       ├─ empty_strided_p2p(...)        ← 每个 rank 分配自己的对称缓冲
       ├─ rendezvous(t, group_name)     ← 组内交换地址,必须全员到场
       ├─ 收集 peer_ptrs / flag_ptrs    ← 之后本 rank 就能直写对端
       └─ 清零自己的握手区 + barrier     ← 保证 epoch 从 0 开始
```

#### 4.2.3 源码精读

注册调用:[python/fast_ulysses/group.py:L80-L85](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L80-L85)。注释说了两个关键事实:注册是**幂等**的;从 torch 2.13 起这个 API 已弃用(rendezvous 会自己解析进程组),但为了兼容 torch 2.10 仍然要调——所以外面套了 `catch_warnings` 压掉 `FutureWarning`。

`allocate()` 的 rendezvous 部分(轮廓,u4 精读):[src/group.cc:L218-L227](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L218-L227)。`empty_strided_p2p` 分配,`rendezvous(t, group_name_)` 按**进程组名字**完成组内地址交换;`TORCH_CHECK(sym, ...)` 的报错消息把两种失败并列出来——"The allocation is not a symmetric one, or the group is not registered with torch's symmetric-memory bootstrap"——第二种正是「某个 rank 忘了构造」的症状。

rendezvous 之后的成员资格交叉验证:[src/group.cc:L232-L244](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L232-L244)。`get_buffer_ptrs()` 返回的指针列表**按构造时的组内 rank 排列**,这里检查它的长度恰好等于 `world_size_`,否则报 "the process group and this group disagree about their membership"——这是「Python 侧的 rank/world_size 与 rendezvous 实际凑齐的成员不一致」的显式防线。最后 [src/group.cc:L250-L251](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L250-L251) 清零自己的握手区并 `sym->barrier(0, 0)`,保证全组的 epoch 计数器统一从零开始。

#### 4.2.4 代码实践

**实践目标**:在源码层面数清构造路径上的集合点,并理解漏掉一个 rank 的后果。

**操作步骤**(源码阅读型实践,不需要 GPU):
1. 读 [group.py:L43-L96](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L43-L96),把每一步标注为「真集合 / 本地但须全组一致 / 纯本地」三类。
2. 读 [src/group.cc:L207-L253](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L207-L253) 的 `allocate()`,找出其中需要全员到场的两处(rendezvous、末尾的 barrier)。
3. 纸面推演:ws=4,rank 2 因为一个只在自己分支里的 `if` 跳过了 `UlyssesGroup(...)` 构造,但其余三个 rank 继续发起第一次 `all_to_all_4d`。写出每个 rank 各自卡在哪里。

**需要观察的现象 / 预期结果**:推演结论——rank 0/1/3 在 `rendezvous` 内部等待第四个参与者;rank 2 独自往下跑或在别处等待。没有任何异常抛出,进程挂起。这正是 docs/api.md 契约一节 "Violating these hangs the group. Nothing raises and nothing times out." 的含义。若想实际复现(可选,待本地验证,需要多卡并手动 kill):写一个 rank 条件跳过构造的脚本,用 `timeout 60 torchrun ...` 兜底,观察它到期被杀而不是报错。

#### 4.2.5 小练习与答案

**练习 1**:构造函数里哪一步是真正的分布式通信?

**答案**:只有 `_require_nvlink` 里的 `dist.all_gather`([group.py:L103](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L103))。`enable_symm_mem_for_group` 是本地注册,C++ 句柄和 comm 流创建也是本地的。

**练习 2**:既然构造不分配窗口,为什么文档还说构造是 collective?

**答案**:两个原因。其一,构造路径上有一次真集合通信(`all_gather`),一个 rank 不构造,其他 rank 就卡在那里。其二,构造把进程组注册进对称内存 bootstrap,这是后续每一次窗口 rendezvous 的前提;漏掉构造的 rank 会让**第一次调用**挂起。所以「构造是集合的」是对整个生命周期负责的表述。

**练习 3**:`allocate()` 末尾为什么必须 `sym->barrier(0, 0)`,能不能省掉?

**答案**:不能。每个 rank 只清零**自己**的握手区(可能复用了旧窗口的脏内存)。若没有 barrier,rank A 可能已经发出第一次 publish,而 rank B 还没清零——那次 publish 落在被清零抹掉的内存里,B 永远等一个「其实已经发生过」的写入。注释原文([src/group.cc:L246-L251](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L246-L251)):"one rank's first publish lands in a pad another has yet to clear, is erased, and that rank waits forever"。

### 4.3 NVLink 检查与失败开放

#### 4.3.1 概念说明

fast-ulysses 的传输方式是「直接写对端的对称内存」。这条路的性能完全依赖 NVLink:同样的直写走 PCIe——尤其还要跨 CPU socket——比 `torch.distributed`(会绕开这条链路走 NIC 或主存)更慢。所以错误拓扑下的库是**正确但更慢**的,用户最难自查。`require_nvlink=True` 的检查就是为此存在:构造时就把这种组拒绝掉。

检查分两层:

- **Python 层(`_require_nvlink`)**:用一次 `all_gather` 把全组 rank 的设备号收集到每个 rank。此后每个 rank 拿着**同一份设备清单**去做本地探测,结论必然一致——文档称之为 "the refusal is collective in practice":要么全组都拒绝,要么全组都通过,不会出现一半 rank 抛异常、另一半在下一个集合点等死锁。
- **C++ 层(`check_nvlink` → `nvlink_matrix`)**:用 NVML 探测这些设备两两之间是否有 NVLink。两种拓扑都算「连接」:GPU-GPU 直连,或双方都挂到同一块 NVSwitch fabric(单机一块 fabric,挂上去即全互连)。

**失败开放(fail-open)** 是本模块最重要的语义:当 NVML 不可用(库缺失、太老、某个设备探测失败)时,检查**放行**而不是拒绝。理由写在头文件里:这个检查的目的是拒绝「正确但慢」的组,属于性能建议而非正确性守卫;拿不到答案时拒绝会误伤所有没装 NVML 的合法 NVLink 机器,而放行的最坏后果只是慢。api.md 的表述([docs/api.md:L30-L34](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md#L30-L34)):"the group is built with no refusal and no warning, so a PCIe group on such a machine runs, slowly, instead of being caught here."

#### 4.3.2 核心流程

```text
_require_nvlink (每个 rank,构造时)
  1. local = [self.device.index]               # 本 rank 的设备号
  2. dist.all_gather(gathered, local, pg)      # ← 全组交换设备号(真集合通信)
  3. devices = 展开成 int 列表
  4. 若 devices 有重复 → return                # 多个 rank 共享一块 GPU:跳过检查
  5. problem = _C.check_nvlink(devices)        # → C++/NVML 探测
  6. problem 非空 → raise RuntimeError(problem)

check_nvlink (C++)
  matrix = nvlink_matrix(devices)
  ├─ NVML 不可用 / 有设备探测失败 → nullopt → 返回 ""(失败开放,放行)
  └─ matrix 就绪 → 逐对检查 (i, j),i≠j 且未连接 → 返回拒绝消息

nvlink_matrix (C++)
  1. dlopen("libnvidia-ml.so.1") + dlsym 解析五个入口点;失败即 nullopt
  2. 对每个设备:cudaGetDeviceProperties 拿 PCI 三元组(domain, bus, device)
     → NVML 按总线 ID 换 handle(BDF 匹配)
  3. 逐设备扫描 0..31 号链路:
     - NOT_SUPPORTED / INVALID_ARGUMENT = "设备答完了" → 标记 answered,停止
     - 链路 active 且对端是 GPU   → BDF 匹配到组内设备 → 记直连
     - 链路 active 且对端是 SWITCH → 记入 on_switch 集合
     - 整个设备一次都没答上话 → 整个矩阵不可信 → nullopt
  4. on_switch 内部的所有设备两两记为连接(fabric 全互连)
```

#### 4.3.3 源码精读

Python 侧的检查入口:[python/fast_ulysses/group.py:L98-L109](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L98-L109)。docstring 一句话讲透设计:"Every rank checks the same set of devices, so the answer is identical on all of them and the refusal is collective in practice." 关键的 all_gather 设备号三行在 [group.py:L101-L104](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L101-L104)。共享 GPU 的跳过在 [group.py:L105-L106](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L105-L106):两个 rank 指向同一块卡时,这不是该检查能讨论的拓扑(那是测试/沙箱场景),直接放行。最后 [group.py:L107-L109](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L107-L109) 把 C++ 返回的拒绝消息原样变成 `RuntimeError`。

为什么是 NVML 而不是 CUDA 的 P2P 查询:[include/fast_ulysses/nvlink.hpp:L1-L8](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/nvlink.hpp#L1-L8)——"NVML is the only interface that reports the link TYPE; `cudaDeviceCanAccessPeer` reports only that P2P works, which is equally true of PCIe." 也就是说 CUDA 的接口无法区分「值得用的互连」和「不值得用的互连」,这个区分只有 NVML 的链路类型信息能做。

NVML 的加载:[src/nvlink.cc:L66-L85](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/nvlink.cc#L66-L85)。`dlopen("libnvidia-ml.so.1", RTLD_LAZY | RTLD_LOCAL)` 而不是链接期依赖——机器上没有 NVML 或版本太老时,扩展照样能加载,探测只是「答不出」(`nullptr` → `nullopt`)。`std::call_once` 保证整个进程只 init 一次(NVML init 幂等但不免费,调用点只有构造和 doctor)。

「设备答完」与「无法询问」的区分:[src/nvlink.cc:L20-L28](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/nvlink.cc#L20-L28)。完全没有 NVLink 的设备对 0 号链路返回 `NOT_SUPPORTED`,有 NVLink 的设备越过最后一条链路返回 `INVALID_ARGUMENT`——**两者都是设备在回答**("到头了"),绝不能与「NVML 用不了」混淆。

BDF 匹配:[src/nvlink.cc:L121-L136](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/nvlink.cc#L121-L136)。CUDA 报告的 PCI 三元组格式化成 NVML 的总线 ID 字符串换 handle;任何一步失败即返回 `nullopt`(整体失败开放,而不是给出一个缺了行的矩阵)。BDF 结构本身在 [src/nvlink.cc:L87-L110](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/nvlink.cc#L87-L110),其中 `device_bdf` 对 `cudaGetDeviceProperties` 失败的处理很有教益:错误被就地消费掉(`cudaGetLastError()`),否则这个 pending error 会从**调用者接下来随便哪个无关 CUDA 调用**里冒出来。

逐设备扫描与 per-device `answered` 标志:[src/nvlink.cc:L145-L190](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/nvlink.cc#L145-L190)。`answered` 标志必须在**每个设备**的循环里重新置 false——注释解释了若做成整个扫描共享一个标志,第一个答上话的设备会替后来所有拒答的设备「背书」,让真实存在的链路被误报为缺失,`check_nvlink` 就会**错误地拒绝**一个合法组——这正是失败开放要防的反面:错误拒绝(false refusal)。直连与 NVSwitch 的分支在 [src/nvlink.cc:L166-L183](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/nvlink.cc#L166-L183):对端类型是 SWITCH 就把本设备记进 `on_switch`,是 GPU 就用对端 BDF 反查组内设备、双向记直连。一个设备始终没答上话则整体作废:[src/nvlink.cc:L187-L189](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/nvlink.cc#L187-L189)——它的行和列会全读作「未连接」,与「真的没有链路」无法区分,宁可不给答案。fabric 全互连补全在 [src/nvlink.cc:L192-L197](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/nvlink.cc#L192-L197)。

`check_nvlink` 本体:[src/nvlink.cc:L201-L220](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/nvlink.cc#L201-L220)。矩阵拿不到就返回空串(放行,[L203-L206](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/nvlink.cc#L203-L206));否则逐对找第一条断开的边,返回的拒绝消息([L207-L217](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/nvlink.cc#L207-L217))内容本身就是一课:点名两块卡、解释**为什么**慢("writes peer memory directly, which over PCIe ... is slower than torch.distributed, because that routes around the link")、给出两条出路(换单机 NVLink 组 / `require_nvlink=False` 测量),并指向 `fast-ulysses doctor` 看全矩阵。

pybind 绑定:[src/bindings.cc:L785-L799](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L785-L799)。`check_nvlink` 供构造使用;`nvlink_matrix` 作为独立函数暴露,Python 侧由 [python/fast_ulysses/__init__.py:L16-L18](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/__init__.py#L16-L18) 导出为 `fast_ulysses.nvlink_matrix`——它**不是集合操作、不需要进程组**([docs/api.md:L36-L40](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md#L36-L40)),`fast-ulysses doctor` 打印的矩阵就是它。

#### 4.3.4 代码实践

**实践目标**:用独立的 `nvlink_matrix` 探测本机拓扑,理解「矩阵 / None / 矩阵里有 False」三种输出的含义。

**操作步骤**(示例代码):

```python
# u2_l1_nvlink.py —— 单进程即可,不需要 torchrun
import torch
from fast_ulysses import nvlink_matrix

print(nvlink_matrix([torch.cuda.current_device()]))       # 单设备
if torch.cuda.device_count() >= 2:
    m = nvlink_matrix([0, 1])                             # 两设备
    print({k: v for k, v in m.items() if k[0] < k[1]})    # 只看上三角
```

再在 shell 里运行 `fast-ulysses doctor`,与 `nvidia-smi topo -m` 的 NV 列对照。

**需要观察的现象**:
1. 单设备调用:返回一个不含任何 `False` 的小矩阵(没有 `i≠j` 的对),或 `None`。
2. 双设备:三种可能——全 `True`(直连或都挂 NVSwitch)、存在 `False`(如 `NV2` 经由 PCIe 的 SMP 组合)、`None`(NVML 不可用)。

**预期结果**:由 [nvlink.cc:L138-L143](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/nvlink.cc#L138-L143) 可推:矩阵对角线恒为 `True`(`linked[{i,j}] = (i==j)`),所以单设备永远查不出问题。本机具体落在哪种情况待本地验证(取决于 NVLink 拓扑与 NVML 可用性)。

#### 4.3.5 小练习与答案

**练习 1**:两块 GPU 通过 NVSwitch 通信,`nvlink_matrix` 怎么知道它们「连接」?它有没有直接查「GPU 到 switch 到 GPU」的完整路径?

**答案**:没有查完整路径。它只验证**两端各自**有一条 active 链路指向 SWITCH 类型对端([nvlink.cc:L166-L172](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/nvlink.cc#L166-L172) 记入 `on_switch`),然后依据「单机一块 fabric,挂上去即两两可达」的假设把 `on_switch` 内部全互连([L192-L197](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/nvlink.cc#L192-L197))。这是拓扑假设,不是逐跳验证。

**练习 2**:共享 GPU 跳过检查的分支,为什么放在 `check_nvlink` 之前(Python 层)而不是 C++ 层?

**答案**:因为判断条件是「设备号列表有重复」,这个列表就是 Python 侧 all_gather 的产物([group.py:L104-L106](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L104-L106))。两个 rank 共享一块卡时,「这些 GPU 是否互连」这个问题没有意义(它们是同一块卡),继续问 NVML 反而可能因重复设备号产生歧义。在信息产生的地方(谁收集了设备号)做判断,是最自然的分层。

**练习 3**:假设把失败开放改成失败关闭(NVML 不可用时拒绝构造),哪类机器会最先遭殃?

**答案**:所有「NVLink 正常但 NVML 缺失或太老」的机器——比如精简的容器镜像没带 `libnvidia-ml.so.1`,或驱动用户态库版本过老缺 `nvlink_remote_pci` 入口。它们本可以正常高速运行,却被一个**性能建议性质**的检查挡在门外。头文件的契约([nvlink.hpp:L23-L25](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/nvlink.hpp#L23-L25))写明:调用方在 `nullopt` 时"has no basis to refuse a group and should say so rather than guess"。

### 4.4 子组支持与 destroy

#### 4.4.1 概念说明

**任意子组可用**是构造 API 的一等承诺([docs/api.md:L26](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md#L26)):"Any subgroup works — there is no restriction on which ranks it contains." 典型场景是 tp × sp 二维并行:sp 维是 `WORLD` 的一个等差切片(如 8 卡 tp=2 时,sp 组是 {0,2,4,6} 和 {1,3,5,7}),每个 sp 组各建一个 `UlyssesGroup`。

子组能工作的三个支撑点:

1. **rank / world_size 取自子组**(`dist.get_rank(pg)`),所以屏障发布、plan 寻址用的都是组内编号;全局 rank 2 在子组 {0,2,4,6} 里的组内编号是 1。
2. **对称内存注册与 rendezvous 都按 `pg.group_name` 寻址**——不同子组名字不同,互不干扰,两个组可以同时各自传输。
3. **NVLink 检查的 all_gather 也跑在子组上**,探测的是子组成员的设备。

`destroy()` 同样是集合操作:它先同步自己的 comm 流,再跨组 `dist.barrier`,最后释放 C++ 资源。顺序的含义是:释放窗口本身是集合性的(对端可能还持有指向你窗口的地址),必须全员到齐一起放。

#### 4.4.2 核心流程

```text
二维并行下的构造(test/distributed/subgroup.py 的真实布局,ws=8, TP=2)

  ranks:      0  1  2  3  4  5  6  7
  sp 组 t=0:  ●  ○  ●  ○  ●  ○  ●  ○     {0,2,4,6},stride 2
  sp 组 t=1:  ○  ●  ○  ●  ○  ●  ○  ●     {1,3,5,7}

  每个 rank:
    sp_pgs = [dist.new_group(ranks=range(t, ws, TP)) for t in range(TP)]
              # new_group 对 WORLD 是集合的 → 所有 rank 都要建出两个组,
              # 再各自保留自己所在的那个
    group = UlyssesGroup(process_group=sp_pgs[rank % TP])
              # 组内编号:rank 2 → group.rank = 1, group.world_size = 4
    ... 两个组同时各自 all_to_all_4d ...
    group.destroy()      # 组内集合
```

#### 4.4.3 源码精读

`rank` / `world_size` 从传入的组推导:[python/fast_ulysses/group.py:L62-L65](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L62-L65)。quickstart 的二维并行示例:[docs/quickstart.md:L80-L88](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/quickstart.md#L80-L88),`mesh["sp"].get_group()` 切出 sp 组直接传入。

子组测试的构造现场:[test/distributed/subgroup.py:L124-L131](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/subgroup.py#L124-L131)。`dist.new_group` 是对 WORLD 的集合操作,所以**每个 rank 都要调用**,各自保留自己所在的那个组(注释:"every rank builds BOTH and keeps the one it is in")。注意该测试传了 `require_nvlink=False`,让这个 worker 在任何机器上都能跑到它真正要检验的东西——成员资格算术,而不是让拓扑检查决定它能否运行。

组内编号正确性的断言:[test/distributed/subgroup.py:L133-L148](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/subgroup.py#L133-L148)。`(group.world_size, group.rank) == (sp, rank // TP)`——如果实现错误地从 WORLD 推导编号,一半的 rank 会立刻不匹配。失败被聚合到全组 `all_reduce` 之后才行动,注释点明原因:一部分 rank 报错退出、另一部分还在等它 barrier,得到的是挂起而非失败。

subgroup.py 的 docstring 还从测试设计角度说明了子组为什么是「容易被悄悄做错」的属性:[test/distributed/subgroup.py:L10-L16](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/subgroup.py#L10-L16)——所有跑在 WORLD 上的测试里组内编号和全局编号是同一个数,混淆两者的构建也能全绿;只有子组场景能把这类 bug 逼成「错误的答案」或「没人给我发数据的挂起」。

`destroy` 的三步:[python/fast_ulysses/group.py:L267-L277](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L267-L277)。先 `self._comm_stream.synchronize()`——`dist.barrier` 只同步调用者当前流,一条没等待的异步调用可能仍在写即将释放的窗口;再 `dist.barrier(group=self.pg)` 全员到齐;最后 `self._handle.destroy()` 进 C++ 释放。`__del__`([L279-L286](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L279-L286))只同步 comm 流、**故意不做 barrier**——"a finalizer is no place for a collective"——所以被丢弃而非 destroy 的组是走完传输安全但没做组内同步的,`destroy()` 才是受支持的路径。

#### 4.4.4 代码实践

**实践目标**:不跑代码,预测子组场景下的编号,并核对测试的真实断言。

**操作步骤**(纸面推演 + 源码核对):
1. 设 ws=8、TP=2,子组按 stride 2 切分。对全局 rank 0、1、2、7 分别写出 `(group.world_size, group.rank)`。
2. 把你的答案与 [subgroup.py:L136](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/subgroup.py#L136) 的表达式 `(sp, rank // TP)` 对照。
3. 再想一步:如果子组改成连续两半 {0,1,2,3} / {4,5,6,7},组 0 的每个成员的组内编号恰好等于全局编号——读 [subgroup.py:L48-L52](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/subgroup.py#L48-L52),说明为什么测试**刻意不用**这种切法。

**需要观察的现象 / 预期结果**:
1. rank 0 → (4, 0);rank 1 → (4, 0)(它在另一个组,组内也是 0 号);rank 2 → (4, 1);rank 7 → (4, 3)。
2. 与 `rank // TP` 一致。
3. 连续切分下组 0 的组内编号 == 全局编号,一个把组内编号错当全局编号用的实现照样通过组 0 的检查,一半的 bug 藏没了——stride 切分让两组的编号都「错位」,混淆必然暴露。

#### 4.4.5 小练习与答案

**练习 1**:`dist.new_group` 为什么要在**所有** rank 上调用,哪怕某 rank 不属于那个子组?

**答案**:`new_group` 本身是对 WORLD 的集合操作(所有进程要就此协商一致)。只让子组成员调用会让其余 rank 卡在这个协商上。所以 subgroup.py 让每个 rank 把两个子组都建出来、各取所需([subgroup.py:L124-L126](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/subgroup.py#L124-L126))。

**练习 2**:`destroy()` 里 `dist.barrier` 之前为什么必须先 `self._comm_stream.synchronize()`?

**答案**:`dist.barrier` 只同步**调用者当前流**。若此前有未等待的异步调用,comm 流可能仍在写某个窗口,而 barrier 之后窗口就要释放了——写入打到已回收的内存。先同步 comm 流保证组内不再有任何在途写入([group.py:L271-L275](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L271-L275) 的注释)。

**练习 3**:两个**重叠**(overlap 而非 partition)的子组各建一个 `UlyssesGroup`,库会拒绝吗?

**答案**:不会在构造时拒绝。subgroup.py 的 docstring 明确记录了这个边界([subgroup.py:L78-L86](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/subgroup.py#L78-L86)):v0.2 的构造里唯一的集合通信就是子组上的 NVLink all_gather,重叠组在构造层面没有新的挂起风险;但调用路径上的集合(窗口分配等)仍遵守普通的嵌套集合排序规则——共享 rank 的两个组必须以相同相对顺序进入各自的集合,和两个重叠的 NCCL 子组一样。违反它的表现是挂起,且本测试套件不覆盖。

## 5. 综合实践

把本讲四个模块串起来:写一个脚本,先在默认 `WORLD` 上构造并销毁一个组,再在显式子组上构造、校验组内编号、销毁——这正是 `test/distributed/subgroup.py` 的骨架去掉它的竞态设计后的最小版本。

```python
# u2_l1_practice.py —— 示例代码(非项目文件)
# 多卡运行:  torchrun --nproc_per_node=4 u2_l1_practice.py
# 单卡运行:  torchrun --nproc_per_node=1 u2_l1_practice.py
import os

import torch
import torch.distributed as dist
from fast_ulysses import UlyssesGroup

dist.init_process_group("nccl")
torch.cuda.set_device(int(os.environ.get("LOCAL_RANK", 0)))
rank, ws = dist.get_rank(), dist.get_world_size()

# ---- 组 1:默认 WORLD ------------------------------------------------------
# require_nvlink 默认 True。构造内部会执行 _require_nvlink,其中收集设备号的关键三行
# 是 python/fast_ulysses/group.py:L101-L104:
#     local    = torch.tensor([self.device.index], ...)   # 本 rank 的设备号
#     gathered = [torch.zeros_like(local) for _ in range(self.world_size)]
#     dist.all_gather(gathered, local, group=self.pg)      # ← 全组交换设备号(集合通信)
# 之后每个 rank 拿同一份 devices 列表去问 NVML,结论全组一致 → 拒绝也是"全组一起拒绝"。
g_world = UlyssesGroup()                     # 集合操作:每个 rank 都要执行到这一行
print(f"[{rank}/{ws}] WORLD ok: device={g_world.device}, "
      f"rank={g_world.rank}, ws={g_world.world_size}", flush=True)

# ---- 组 2:显式子组 --------------------------------------------------------
if ws >= 2:
    # dist.new_group 对 WORLD 是集合的:所有 rank 都要调用,再各自保留自己所在的组
    # (与 test/distributed/subgroup.py 的做法一致)。
    sub_ranks = list(range(0, ws, 2))        # 偶数全局 rank 的 stride-2 子组
    sub_pg = dist.new_group(ranks=sub_ranks)
    if rank in sub_ranks:
        # 多卡机器若不是全 NVLink 互连,这里可加 require_nvlink=False 以便先跑通流程;
        # 生产/正式测量时保持默认 True,让错误拓扑在构造期暴露。
        g_sub = UlyssesGroup(process_group=sub_pg)
        # rank/world_size 是"组内"编号:全局 rank 2 在 {0,2,...} 里的组内编号是 1
        expect = (len(sub_ranks), sub_ranks.index(rank))
        assert (g_sub.world_size, g_sub.rank) == expect, \
            f"got {(g_sub.world_size, g_sub.rank)}, want {expect}"
        print(f"[{rank}/{ws}] subgroup ok: ({g_sub.world_size}, {g_sub.rank})", flush=True)
        g_sub.destroy()                      # destroy 也是集合的:子组内所有成员一起调用
        print(f"[{rank}/{ws}] subgroup destroyed", flush=True)
else:
    # 单卡(world_size=1):WORLD 本身就是 1-rank 组,构造/销毁同样成立。
    # _require_nvlink 仍会 all_gather(只有一个参加者),devices 只有一个元素,
    # 没有 (i, j) 对可查,check_nvlink 必然放行 —— ws=1 是合法一等公民。
    print(f"[{rank}/{ws}] ws=1: WORLD-only path", flush=True)

g_world.destroy()                            # 与所有 WORLD 成员一起释放
dist.destroy_process_group()
print(f"[{rank}/{ws}] done", flush=True)
```

**操作步骤**:
1. 多卡机器:`torchrun --nproc_per_node=4 u2_l1_practice.py`;单卡机器:`torchrun --nproc_per_node=1 u2_l1_practice.py`(走 ws=1 分支)。
2. 跑之前先 `fast-ulysses doctor` 看一眼 NVLink 矩阵,预判构造会不会被拒。
3. 对照阅读 [test/distributed/subgroup.py:L110-L148](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/subgroup.py#L110-L148),比较它与你脚本的差异(它的 stride-2 布局、`require_nvlink=False`、把失败聚合成 `all_reduce` 再统一退出)。

**需要观察的现象**:
1. WORLD 组构造打印的 `device` / `rank` / `world_size` 与 torchrun 拓扑一致。
2. 子组成员打印的 `(world_size, rank)` 是**组内**编号(全局 rank 2 → `(2, 1)`,ws=4 时)。
3. 非 subgroup 成员(奇数 rank)不打印 "subgroup ok",但顺利走到 "done"。
4. 单卡运行打印 "ws=1: WORLD-only path",全程无异常。

**预期结果**:以上均可从源码直接推出。真实多卡输出待本地验证;若你的机器 GPU 未全互 NVLink,默认 `require_nvlink=True` 的 WORLD 构造会抛 `RuntimeError`,消息里点名两块卡并建议 `fast-ulysses doctor`——这本身就是本讲 4.3 的现场演示。

## 6. 本讲小结

- 构造三参数:`process_group`(`None` → `WORLD`,任意子组可用)、`device`(`None` → 当前设备,统一归一化为带 index 的 `torch.device`)、`require_nvlink`(默认 `True`,仅关键字,拒绝非 NVLink 组)。
- 构造是集合操作:唯一的真集合通信是 NVLink 检查里的设备号 `all_gather`;`enable_symm_mem_for_group` 是本地但人人必须的注册,为第一次窗口分配的 rendezvous 铺路;违反的后果是挂起而非报错。
- NVLink 检查两层结构:Python 收集设备号保证「全组同一份输入 → 同一个结论」,C++ 用 `dlopen` 的 NVML 按 BDF 匹配设备、识别直连与 NVSwitch 两种拓扑;两个 rank 共享一块 GPU 时跳过。
- 检查是失败开放的:NVML 缺失、太老或某设备拒答时放行不拒绝——它是性能守卫而非正确性守卫,误拒(比如共享 `answered` 标志导致的假缺失链路)比放行更有害。
- 子组是按名字寻址的独立宇宙:`rank` / `world_size` 是组内编号,rendezvous、屏障、NVLink all_gather 都跑在子组上;`destroy()` 先同步 comm 流再组内 barrier,是受支持的收尾路径。

## 7. 下一步学习建议

下一讲 **u2-l2(all_to_all_4d 的形状契约与错误检查)** 进入构造之后的第一次真实调用:mode 与输入输出形状的对应、dtype 白名单与 16 字节对齐、以及「校验必须先于第一个屏障」的原则——你会看到本讲 `__del__`/构造里反复出现的「拒绝要干净、要全组一致」如何延续成完整的校验流水线。

源码阅读方向:想深挖本讲两条线索的任一条,可以提前看 [src/group.cc 的 allocate()](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L207-L253)(u4-l1 的主题,对称内存窗口的分配与握手区布局)或 [src/nvlink.cc 的 doctor 输出侧](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/cli.py)(u8-l1 的主题,探测工程化成 CLI)。
