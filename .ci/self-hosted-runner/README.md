# 一次性审查 runner —— 部署件

在内网 mac-mini 上跑 PR 自动审查：启动器循环起一次性容器，容器里就是 GitHub 官方 runner
加审查环境；容器只有一个出口（宿主机），到内网和公网都没有路由。

## 这个形状的由来

- **容器由启动器起，不由任务起**——任务对容器形态没有话语权，提不了特权、挂不了宿主目录。
- **每个容器跑在自己的轻量 VM 里**（Apple `container` 的默认形态），任务之间、任务与 mac 之间独立内核。
- **出口只有宿主机**：容器接在 `--internal`（host-only）网络上，实测连公网、内网他机、同网段网关全不通，
  唯一能到的是宿主机网关。所以 GitHub 流量和模型流量都必须经宿主机的代理出去。
- **真 key 不进容器**：模型真 key 只在宿主机网关 `gateway.py` 手里，容器拿到的是一把假钥匙。

## 目录

```
prreview/
├── prrlib.py               共用：日志配置、配置读取、来源网段判断
├── gateway.py              宿主机网关（一个进程两个端口）
│                             18080 按域名白名单转发 GitHub
│                             18081 把假钥匙换成真 key 后转发模型
├── launcher.py             启动器：池子 + 启动前自检 + 内存准入 + 定期清理
├── fetch_image.py          从 CI 的 artifact 取镜像并 load（不需要 registry 凭据）
├── pf-setup.sh             装宿主机侧出口过滤（容器只能到宿主机）
├── prreview.json           唯一配置文件（不含凭据）
├── probe.sh                隔离探针（可手工跑，也可以在 workflow 里跑）
├── install-launchd.sh      装两个 LaunchAgent（gateway + launcher）
├── container/Dockerfile.runner   本地构建用的底座（CI 构建见 .ci/Dockerfile.pr-runner）
├── secrets/                chmod 600，全部不进 git
│   ├── github-token        能申请 runner 登记凭据的那把凭据
│   ├── model-key           模型真 key（只在宿主机网关手里）
│   └── container-token     给容器的假钥匙
└── logs/
```

## 装法

1. **装 Apple container**（需要管理员密码）：
   ```bash
   curl -fL -o /tmp/container.pkg \
     https://github.com/apple/container/releases/download/1.5.0/container-1.5.0-installer-signed.pkg
   sudo installer -pkg /tmp/container.pkg -target /
   container system start
   ```
   如果 buildkit 报 `Rosetta is not installed`，写 `~/.config/container/config.toml`：
   ```toml
   [build]
   rosetta = false
   ```
   然后 `container system stop && container system start`。

2. **建网络**：`container network create --internal --subnet 192.168.100.0/24 prr-int`

3. **放凭据**（`~/prreview/secrets/`，全部 `chmod 600`）：见上表。

4. **取镜像**：`python3 ~/prreview/fetch_image.py`
   （走 GitHub 仓库的 artifact，不需要 registry 凭据。若把 ghcr 上的包设成 public，
   也可以直接 `container image pull ghcr.io/<owner>/prr-review:latest`——那条能按 digest 钉住。）

5. **装出口过滤**（关键，见下节）：`bash ~/prreview/pf-setup.sh`
   然后 `bash ~/prreview/pf-setup.sh --persist`（macOS 开机不自动启用 pf）

6. **装服务**：`bash ~/prreview/install-launchd.sh`

## 出口过滤的必要性

Apple container 的 `--internal`（hostOnly）网络**不封宿主机**——官方对 hostOnly 的定义就是
"同一子网内互通"，而宿主就是那个子网的网关。另外上游还有个 open bug（apple/container#2062）：
hostOnly 网络里出站 TCP 是否被转发，取决于宿主机的 `net.inet.ip.forwarding`；这台机器上它是 **1**。

从 container 的设置里关不掉（`NetworkMode` 只有 `nat` / `hostOnly` 两个值，没有网关/NAT 开关；
`--option mode=isolated` 会被接受但忽略）。所以唯一能收口的位置是宿主机。

`pf-setup.sh` 在宿主上装一层只匹配 `prr-int` 网段的规则：

```
pass  quick proto udp from 192.168.100.0/24 port 68 to any port 67
pass  quick proto tcp from 192.168.100.0/24 to any port { 18080, 18081 } flags S/SA keep state
block drop quick from 192.168.100.0/24 to any
```

规则以「源 = 该网段」为条件，机器上其它容器在别的网段，**一条都不匹配**。
`launcher.py` 起 runner 之前会实测一次（能不能碰到宿主机上与本方案无关的服务端口），封不住就拒绝干活。

## 已知缺口（别当已经解决）

- **出口过滤依赖 pf 一直在位**。规则本身由 `/etc/pf.anchors/prreview` 提供、`/etc/pf.conf` 引用，
  开机生效要装 `--persist` 那个 LaunchDaemon。`launcher.py` 有自检兜底（封不住就不起容器），
  但那是发现，不是阻止。
- **长期凭据的权限**：`secrets/github-token` 必须是"只够本仓注册 runner"的最小权限，
  别用宽口径的 OAuth token。
- **假钥匙会进容器记录**：`ANTHROPIC_API_KEY`（给容器的那把假钥匙）走环境变量，
  因而会出现在容器的 `config.json` 里。这条是有意接受的——它是我们自己造的、
  只在本地代理上有效，而代理本身还限了来源网段。真凭据一律不走环境变量。

## 登记凭据进容器的方式

一次性登记凭据不能随便传，四条更顺手的路都试过并否掉：

| 路径 | 不行的原因 |
|---|---|
| `container run ... --jitconfig <blob>` | 凭据进宿主机的进程命令行，`ps aux` 人人可见 |
| `container run -e PRR_JITCONFIG` | 容器运行时把它写进 `containers/<name>/config.json`（644），**且删容器不删记录** |
| 拷进容器内的 tmpfs | `container cp` 报告成功，但文件**穿不进运行中 VM 的挂载点** |
| 走 stdin | `container run` **不转发 stdin** |

现在走的是 `container cp` 拷进容器文件系统：宿主侧只在 `mkstemp` 建的 0600 临时文件里过一下、
随即 unlink；容器侧由入口包装读完立刻删。实测宿主 `ps` 里搜不到、容器记录里没有、删容器无残留。


## 启动器的行为

池子模式：同时维持 `replicas` 个待命容器（每个一次性 runner 只接一个任务，所以并发 >1 才扛得住排队）。

起容器之前**两项自检**，任一不过就停掉待命容器且不再起新的、并报警：

- **出口自检（安全）**：起一个一次性容器，实测还能不能碰到宿主机上与本方案无关的服务端口 —— 碰得到说明 pf 那层不在了。
- **网关自检（可用性）**：两个端口有没有在监听 —— 不在的话容器会注册成功、领到任务、然后在审查时连不上模型，白吃一个任务。

补新槽之前还看**宿主内存水位**：可回收低于 `min_free_gb` 就不补。macOS 是先压缩后换页，不会崩，但审查是延迟敏感的。

每小时一次**定期清理**：删已停止的容器、删无引用的镜像（**只在池子非空时做**，否则会把自己的镜像清掉）、删"离线且容器已不存在"的 runner 记录。

> ⚠️ 容器在 `container system df` / `du` 里报的磁盘占用是**克隆+稀疏的账面数**，不是它独占的盘。
> 决定性实测：删掉一个容器，卷上实际用量**一点没降**（0.00 GB）。所以"槽位数 × 2.4 GB"这个乘法是错的
> —— 真正占盘的是**镜像堆积**（每次 CI 重建 +约 2.7 GB），这也是定期清理针对的东西。看容量请用 `df`，别用 `container system df`。

## 提交上游前的待改项（上游那个仓是公开的）

公开仓里的注释不该带内网/公司特有的东西。提交前跑：

```
bash check-public-safe.sh <仓库路径> <基线 ref>
```

它只扫**这个分支改动的文件**（不是全仓 —— 全仓里"内网 SSRF""自签证书"这类词本来就有，
扫全仓是在噪音里找信号），分"必须没有"和"需要过目"两档报告。

**已经踩过一次的教训**（都已清掉）：把"内网把 claude.ai 指到自签 ALB"这种**网络结构**
写进了公开仓的 Dockerfile 注释；把**真实内网 IP** `10.108.154.89` 写进了探针的靶子里。
前者泄漏的是我们的网络结构，后者直接给了一个内网地址。

当前已知的、需要在正式上游 PR 里改掉的：

| 项 | 现状 | 上游应改成 |
|---|---|---|
| 构建镜像的触发分支 | `branches: [prr-smoke]`（我们测试用的分支） | `main`，或纯 `workflow_dispatch` |
| 镜像名 | `prr-review` | 跟随上游命名，或确认沿用 |
| 探针里的容器网关 | 写死 `192.168.100.1` | 确认部署网段；或改成仓库变量 |
| 内网 TLS 的说法 | **已清**（原来在两处注释里） | —— |
| 真实内网 IP | **已清**（原来在探针的靶子里） | —— |

## 重启启动器是安全的：正在跑任务的容器不会被杀

收尾时**只收空闲容器**，正在跑任务的放它跑完 —— 一次性 runner 跑完一个任务就自己退出并注销，
所以"放它跑完"是有界的（上界就是 job 本身的超时）。

**不能杀的原因**：掐断那个任务只是其一。更麻烦的是 GitHub **拒绝删除"正在跑任务的 runner"记录**
（422 `currently running a job`），那条记录会一直占着它所在的 concurrency 组，把同一 PR 的
后续 run **全堵在 pending**，直到 job 自己超时。实测踩过一次：任务跑 11 秒时重启，之后卡了近一小时。

而且**没有快路**：删记录被 422 拒、`cancel` 送不到（runner 已经死了），只能等。

万一还是产生了这样的孤儿记录（容器被别的方式杀掉、机器断电等）：

- job 的 `timeout-minutes` 就是它的上界 —— 调小它，孤儿清得更快；
- 期间那个 PR 的后续 run 会**排队而不是失败**，超时后自动放行；
- **不占计算资源**：容器早就销毁了，那条记录只是 offline 的占位，占的只是"排期"。
