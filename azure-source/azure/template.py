"""Build the reviewed ARM template; never contains private keys or OAuth data."""
import base64
from pathlib import Path


def template(region, size, label, public_key, zone='', admin_cidr=''):
    network='2024-05-01';compute='2024-07-01'
    resources=[]
    def add(kind,name,properties,depends=(),**extra):
        resources.append(dict(type=kind,apiVersion=compute if kind.startswith('Microsoft.Compute') else network,
            name=name,location=region,properties=properties,dependsOn=list(depends),**extra))
    def rid(kind,name):return "[resourceId('"+kind+"','"+name+"')]"
    add('Microsoft.Network/networkSecurityGroups','drivecast-nsg',dict(securityRules=[dict(name='Web',properties=dict(
        protocol='Tcp',sourcePortRange='*',destinationPortRanges=['80','443'],sourceAddressPrefix='Internet',
        destinationAddressPrefix='*',access='Allow',priority=100,direction='Inbound'))]))
    if admin_cidr:
        import ipaddress
        network_address=ipaddress.ip_network(admin_cidr)
        if network_address.version!=4 or network_address.prefixlen!=32: raise ValueError('SSH requires one operator IPv4 /32.')
        resources[-1]['properties']['securityRules'].append(dict(name='OperatorSSH',properties=dict(protocol='Tcp',
            sourcePortRange='*',destinationPortRange='22',sourceAddressPrefix=admin_cidr,destinationAddressPrefix='*',
            access='Allow',priority=110,direction='Inbound')))
    add('Microsoft.Network/virtualNetworks','drivecast-vnet',dict(addressSpace={'addressPrefixes':['10.73.0.0/16']},
        subnets=[dict(name='app',properties=dict(addressPrefix='10.73.1.0/24'))]))
    add('Microsoft.Network/publicIPAddresses','drivecast-ip',dict(publicIPAllocationMethod='Static',publicIPAddressVersion='IPv4',
        dnsSettings={'domainNameLabel':label}),sku={'name':'Standard'},**({'zones':[zone]} if zone else {}))
    add('Microsoft.Network/networkInterfaces','drivecast-nic',dict(networkSecurityGroup={'id':rid('Microsoft.Network/networkSecurityGroups','drivecast-nsg')},
        ipConfigurations=[dict(name='app',properties=dict(privateIPAllocationMethod='Dynamic',
            subnet={'id':"[resourceId('Microsoft.Network/virtualNetworks/subnets','drivecast-vnet','app')]"},
            publicIPAddress={'id':rid('Microsoft.Network/publicIPAddresses','drivecast-ip')}))]),
        [rid('Microsoft.Network/networkSecurityGroups','drivecast-nsg'),rid('Microsoft.Network/virtualNetworks','drivecast-vnet'),rid('Microsoft.Network/publicIPAddresses','drivecast-ip')])
    init=base64.b64encode(Path(__file__).with_name('cloud-init.yml').read_bytes()).decode()
    add('Microsoft.Compute/virtualMachines','drivecast-primary',dict(hardwareProfile={'vmSize':size},
        securityProfile=dict(securityType='TrustedLaunch',uefiSettings=dict(secureBootEnabled=True,vTpmEnabled=True)),
        storageProfile=dict(imageReference=dict(publisher='Canonical',offer='ubuntu-24_04-lts',sku='server',version='latest'),
            osDisk=dict(createOption='FromImage',diskSizeGB=64,deleteOption='Detach',managedDisk={'storageAccountType':'StandardSSD_LRS'})),
        osProfile=dict(computerName='drivecast-primary',adminUsername='drivecast',customData=init,
            linuxConfiguration=dict(disablePasswordAuthentication=True,provisionVMAgent=True,
                ssh={'publicKeys':[dict(path='/home/drivecast/.ssh/authorized_keys',keyData=public_key)]})),
        networkProfile={'networkInterfaces':[dict(id=rid('Microsoft.Network/networkInterfaces','drivecast-nic'),properties={'deleteOption':'Detach'})]}),
        [rid('Microsoft.Network/networkInterfaces','drivecast-nic')],identity={'type':'SystemAssigned'},**({'zones':[zone]} if zone else {}))
    return {'$schema':'https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#','contentVersion':'1.0.0.0','resources':resources,
        'outputs':dict(host=dict(type='string',value="[reference(resourceId('Microsoft.Network/publicIPAddresses','drivecast-ip')).dnsSettings.fqdn]"),
            identity=dict(type='string',value="[reference(resourceId('Microsoft.Compute/virtualMachines','drivecast-primary'),'2024-07-01','Full').identity.principalId]"))}
