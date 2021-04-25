import subprocess
import textwrap

import click
from PyInquirer import prompt

from ...tf import generate_cidr_block, var, var_module
from ..provider import *
from .azure_ts import *


class AZURE(Provider):
    """Azure Cloud Provider."""

    def __init__(self, config: SimpleNamespace) -> None:
        config.root_dir = os.path.join(
            str(Path.home()),
            ".pygrid",
            "apps",
            str(config.provider),
            str(config.app.name),
            str(vars(config.app).get("id", "")),
        )
        super().__init__(config.root_dir, "azure")
        self.config = config
        self.name = self.config.app.name + str(vars(config.app).get("id", ""))

        ##TODO(amr): terrascript does not support azurem right now
        # self.tfscript += terrascript.terraform(backend=terrascript.backend("azurerm"))
        self.tfscript += terrascript.terraform(
            required_providers={
                "azurerm": {"source": "hashicorp/azurerm", "version": "=2.46.0"}
            }
        )
        self.tfscript += azurerm(
            features={},
            subscription_id=self.config.azure.subscription_id,
            client_id=self.config.azure.client_id,
            client_secret=self.config.azure.client_secret,
            tenant_id=self.config.azure.tenant_id,
        )

        self.worker = config.app.name == "worker"

        ## TODO
        # if self.worker:
        #     self.build_resource_group()
        #     self.build_security_groups()
        #     self.build_network_interface()
        #     self.build_instances()
        # else:
        self.build_resource_group()
        self.build_security_groups()
        self.build_network()
        self.build_network_interface()

        self.build_instances()

    def build_resource_group(self):
        self.resource_group = azurerm_resource_group(
            f"pygrid_resource_group_{self.name}",
            name=f"pygrid_resource_group_{self.name}",
            location=self.config.azure.location,
        )
        self.tfscript += self.resource_group

    def build_network(self):
        self.network = azurerm_virtual_network(
            f"pygrid_network_{self.name}",
            name=f"pygrid_network_{self.name}",
            address_space=["10.0.0.0/16"],
            resource_group_name=var(
                f"azurerm_resource_group.pygrid_resource_group_{self.name}.name"
            ),
            location=self.resource_group.location,
            tags={
                "name": "pygrid-virtual-network",
                "environment": "dev",
            },
        )
        self.tfscript += self.network

        self.subnets = azurerm_subnet(
            f"pygrid_network_subnet_{self.name}",
            name=f"pygrid_network_subnet_{self.name}",
            resource_group_name=var(
                f"azurerm_resource_group.pygrid_resource_group_{self.name}.name"
            ),
            virtual_network_name=var(
                f"azurerm_virtual_network.pygrid_network_{self.name}.name"
            ),
            address_prefixes=["10.0.2.0/24"],
        )
        self.tfscript += self.subnets

    def build_network_interface(self):
        self.public_ip = azurerm_public_ip(
            f"pygrid_network_public_ip_{self.name}",
            name=f"pygrid_network_public_ip_{self.name}",
            resource_group_name=var(
                f"azurerm_resource_group.pygrid_resource_group_{self.name}.name"
            ),
            location=self.resource_group.location,
            allocation_method="Dynamic",
        )
        self.tfscript += self.public_ip

        self.nif = azurerm_network_interface(
            f"pygrid_network_interface_{self.name}",
            name=f"pygrid_network_interface_{self.name}",
            resource_group_name=var(
                f"azurerm_resource_group.pygrid_resource_group_{self.name}.name"
            ),
            location=self.resource_group.location,
            ip_configuration={
                "name": "internal",
                "subnet_id": var(
                    f"azurerm_subnet.pygrid_network_subnet_{self.name}.id"
                ),
                "private_ip_address_allocation": "Dynamic",
                "public_ip_address_id": var(
                    f"azurerm_public_ip.pygrid_network_public_ip_{self.name}.id"
                ),
            },
        )
        self.tfscript += self.nif

        self.nif_association = azurerm_network_interface_security_group_association(
            f"pygrid_nif_association_{self.name}",
            # name=f"pygrid_nif_association",
            network_interface_id=var(
                f"azurerm_network_interface.pygrid_network_interface_{self.name}.id"
            ),
            network_security_group_id=var(
                f"azurerm_network_security_group.pygrid_network_security_group_{self.name}.id"
            ),
        )
        self.tfscript += self.nif_association

    def build_security_groups(self):
        self.network_security_group = azurerm_network_security_group(
            f"pygrid_network_security_group_{self.name}",
            name=f"pygrid_network_security_group_{self.name}",
            resource_group_name=var(
                f"azurerm_resource_group.pygrid_resource_group_{self.name}.name"
            ),
            location=self.resource_group.location,
            tags={
                "name": "pygrid-security-groups",
                "environment": "dev",
            },
        )
        self.tfscript += self.network_security_group

        security_rules = [
            ("HTTPS", 100, 443, 443),
            ("HTTP", 101, 80, 80),
            ("PyGrid_Domains", 102, 5000, 5999),
            ("PyGrid_Workers", 103, 6000, 6999),
            ("PyGrid_Networks", 104, 7000, 7999),
            ("SSH", 105, 22, 22),
        ]
        for name, priority, source_port, dest_port in security_rules:
            self.tfscript += azurerm_network_security_rule(
                name,
                name=name,
                priority=priority,
                direction="Inbound",
                access="Allow",
                protocol="Tcp",
                source_port_range=source_port,
                destination_port_range=dest_port,
                source_address_prefix="*",
                destination_address_prefix="*",
                resource_group_name=var(
                    f"azurerm_resource_group.pygrid_resource_group_{self.name}.name"
                ),
                network_security_group_name=var(
                    f"azurerm_network_security_group.pygrid_network_security_group_{self.name}.name"
                ),
            )

    def build_instances(self):
        name = self.name

        self.instances = []
        for count in range(self.config.app.count):
            app = self.config.apps[count]

            instance = azurerm_linux_virtual_machine(
                f"pygrid_{name}_instance_{count}",
                name=f"pygrid_{name}_instance_{count}",
                computer_name="UbuntuServer",
                resource_group_name=var(
                    f"azurerm_resource_group.pygrid_resource_group_{self.name}.name"
                ),
                location=self.resource_group.location,
                # vm_os_simple="UbuntuServer",
                size=self.config.azure.vm_size,
                source_image_reference={
                    "publisher": "Canonical",
                    "offer": "UbuntuServer",
                    "sku": "16.04-LTS",
                    "version": "latest",
                },
                os_disk={
                    "caching": "ReadWrite",
                    "storage_account_type": "Standard_LRS",
                },
                network_interface_ids=[
                    var(
                        f"azurerm_network_interface.pygrid_network_interface_{self.name}.id"
                    )
                ],
                custom_data=var(
                    'filebase64("{}")'.format(
                        self.write_domain_exec_script(app, index=count)
                        if not self.worker
                        else self.write_worker_exec_script(app)
                    )
                ),
                admin_username="pygriduser",
                admin_password="pswd123!",
                disable_password_authentication=False,
            )

            self.tfscript += instance
            self.tfscript += terrascript.Output(
                f"instance_{count}_endpoint",
                value=var(instance.public_ip_address),
                description=f"The public IP address of #{count} instance.",
            )
            self.instances.append(instance)

    def build_load_balancer(self):
        self.load_balancer = mylb(
            f"pygrid_load_balancer",
            source="Azure/loadbalancer/azurerm",
            resource_group_name=self.resource_group.name,
            prefix="terraform-lb",
            depends_on=[f"azurerm_resource_group.pygrid_resource_group_{self.name}"],
            frontend_subnet_id=self.subnets.id,
            remote_port={"ssh": ["Tcp", "22"]},
            lb_port={
                "http": ["80", "Tcp", "80"],
                "https": ["443", "Tcp", "443"],
            },
            lb_probe={
                "http": ["Tcp", "80", ""],
                "http2": ["Http", "1443", "/"],
            },
        )
        self.tfscript += self.load_balancer
        self.tfscript += terrascript.Output(
            f"load_balancer_dns",
            value=var_module(self.load_balancer, "azurerm_public_ip_address"),
            description="The DNS name of the ELB.",
        )

    def write_domain_exec_script(self, app, index=0):
        ##TODO(amr): remove `git checkout pygrid_0.3.0` after merge
        branch = "dev"

        # exec_script = "#cloud-boothook\n#!/bin/bash\n"
        exec_script = "#!/bin/bash\n"
        exec_script += textwrap.dedent(
            f"""
            ## For debugging
            # redirect stdout/stderr to a file
            exec &> logs.out
            echo 'Simple Web Server for testing the deployment'
            sudo apt update -y
            sudo apt install apache2 -y
            sudo systemctl start apache2
            echo '<h1>OpenMined {self.config.app.name} Server ({index}) Deployed via Terraform</h1>' | sudo tee /var/www/html/index.html

            echo 'Setup Miniconda environment'
            sudo wget https://repo.continuum.io/miniconda/Miniconda3-latest-Linux-x86_64.sh -O miniconda.sh
            sudo bash miniconda.sh -b -p miniconda
            sudo rm miniconda.sh
            export PATH=/miniconda/bin:$PATH > ~/.bashrc
            conda init bash
            source ~/.bashrc
            conda create -y -n pygrid python=3.7
            conda activate pygrid

            echo 'Install poetry...'
            pip install poetry

            echo 'Install GCC'
            sudo apt-get install zip unzip -y
            sudo apt-get install python3-dev -y
            sudo apt-get install libevent-dev -y
            sudo apt-get install gcc -y

            curl -fsSL https://apt.releases.hashicorp.com/gpg | sudo apt-key add -
            sudo apt-add-repository "deb [arch=amd64] https://apt.releases.hashicorp.com $(lsb_release -cs) main" -y
            sudo apt-get update -y && sudo apt-get install terraform -y

            echo "Setting environment variables"
            export CLOUD_PROVIDER={self.config.provider}

            echo "Exporting Azure Configs"
            export location={self.config.azure.location}
            export subscription_id={self.config.azure.subscription_id}
            export client_id={self.config.azure.client_id}
            export client_secret={self.config.azure.client_secret}
            export tenant_id={self.config.azure.tenant_id}

            echo 'Cloning PyGrid'
            git clone https://github.com/OpenMined/PyGrid && cd /PyGrid/
            git checkout {branch}

            cd /PyGrid/apps/{self.config.app.name}

            echo 'Installing {self.config.app.name} Dependencies'
            poetry install

            ## TODO(amr): remove this after poetry updates
            pip install pymysql

            nohup ./run.sh --port {app.port}  --host 0.0.0.0 --start_local_db
            """
        )

        file_path = os.path.join(str(Path.home()), ".pygrid", "exec_script.txt")
        with open(file_path, "w") as f:
            f.writelines(exec_script)

        return file_path

    def write_worker_exec_script(self, app):
        branch = "dev"
        exec_script = "#!/bin/bash\n"
        exec_script += textwrap.dedent(
            f"""
            exec &> logs.out
            sudo apt update -y

            echo 'Setup Miniconda environment'
            sudo wget https://repo.continuum.io/miniconda/Miniconda3-latest-Linux-x86_64.sh -O miniconda.sh
            sudo bash miniconda.sh -b -p miniconda
            sudo rm miniconda.sh
            export PATH=/miniconda/bin:$PATH > ~/.bashrc
            conda init bash
            source ~/.bashrc
            conda create -y -n pygrid python=3.7
            conda activate pygrid

            echo 'Install poetry...'
            pip install poetry

            echo 'Install GCC'
            sudo apt-get install zip unzip -y
            sudo apt-get install python3-dev -y
            sudo apt-get install libevent-dev -y
            sudo apt-get install gcc -y

            echo 'Cloning PyGrid'
            git clone https://github.com/OpenMined/PyGrid && cd /PyGrid/
            git checkout {branch}

            cd /PyGrid/apps/worker
            echo 'Installing worker Dependencies'
            poetry install
            nohup ./run.sh --port {app.port}  --host 0.0.0.0
            """
        )

        root_dir = os.path.join(str(Path.home()), ".pygrid")
        os.makedirs(root_dir, exist_ok=True)
        file_path = os.path.join(root_dir, "exec_script.txt")
        with open(file_path, "w") as f:
            f.writelines(exec_script)

        return file_path