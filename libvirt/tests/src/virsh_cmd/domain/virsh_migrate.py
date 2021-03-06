import logging
import os
import re
import time
import codecs

from autotest.client.shared import error
from autotest.client.shared import utils

from virttest import utils_test
from virttest import virsh
from virttest import utils_libvirtd
from virttest.libvirt_xml import vm_xml


def run(test, params, env):
    """
    Test virsh migrate command.
    """

    def check_vm_state(vm, state):
        """
        Return True if vm is in the correct state.
        """
        actual_state = vm.state()
        if cmp(actual_state, state) == 0:
            return True
        else:
            return False

    def cleanup_dest(vm, src_uri=""):
        """
        Clean up the destination host environment
        when doing the uni-direction migration.
        """
        logging.info("Cleaning up VMs on %s" % vm.connect_uri)
        try:
            if virsh.domain_exists(vm.name, uri=vm.connect_uri):
                vm_state = vm.state()
                if vm_state == "paused":
                    vm.resume()
                elif vm_state == "shut off":
                    vm.start()
                vm.destroy(gracefully=False)

                if vm.is_persistent():
                    vm.undefine()

        except Exception, detail:
            logging.error("Cleaning up destination failed.\n%s" % detail)

        if src_uri:
            vm.connect_uri = src_uri

    def do_migration(delay, vm, dest_uri, options, extra):
        logging.info("Sleeping %d seconds before migration" % delay)
        time.sleep(delay)
        # Migrate the guest.
        successful = vm.migrate(
            dest_uri, options, extra, True, True).exit_status
        logging.info("successful: %d", successful)
        if int(successful) != 0:
            logging.error("Migration failed for %s." % vm_name)
            return False

        if options.count("dname") or extra.count("dname"):
            vm.name = extra.split()[1].strip()

        if vm.is_alive():  # vm.connect_uri was updated
            logging.info("Alive guest found on destination %s." % dest_uri)
        else:
            logging.error("VM not alive on destination %s" % dest_uri)
            return False

        # Throws exception if console shows panic message
        vm.verify_kernel_crash()
        return True

    vm_name = params.get("migrate_main_vm")
    vm = env.get_vm(vm_name)
    vm.verify_alive()

    # For safety reasons, we'd better back up  xmlfile.
    orig_config_xml = vm_xml.VMXML.new_from_inactive_dumpxml(vm_name)
    if not orig_config_xml:
        logging.error("Backing up xmlfile failed.")

    src_uri = params.get("virsh_migrate_connect_uri")
    dest_uri = params.get("virsh_migrate_desturi")
    # Identify easy config. mistakes early
    warning_text = ("Migration VM %s URI %s appears problematic "
                    "this may lead to migration problems. "
                    "Consider specifying vm.connect_uri using "
                    "fully-qualified network-based style.")

    if src_uri.count('///') or src_uri.count('EXAMPLE'):
        raise error.TestNAError(warning_text % ('source', src_uri))

    if dest_uri.count('///') or dest_uri.count('EXAMPLE'):
        raise error.TestNAError(warning_text % ('destination', dest_uri))

    vm_ref = params.get("vm_ref", vm.name)
    options = params.get("virsh_migrate_options")
    extra = params.get("virsh_migrate_extra")
    delay = int(params.get("virsh_migrate_delay", 10))
    status_error = params.get("status_error", 'no')
    libvirtd_state = params.get("virsh_migrate_libvirtd_state", 'on')
    src_state = params.get("virsh_migrate_src_state", "running")
    migrate_uri = params.get("virsh_migrate_migrateuri", None)
    shared_storage = params.get("virsh_migrate_shared_storage", None)
    dest_xmlfile = ""

    # Direct migration is supported only for Xen in libvirt
    if options.count("direct") or extra.count("direct"):
        if params.get("driver_type") is not "xen":
            raise error.TestNAError("Direct migration is supported only for "
                                    "Xen in libvirt.")

    if options.count("compressed") and not \
            virsh.has_command_help_match("migrate", "--compressed"):
        raise error.TestNAError("Do not support compressed option on this version.")

    # Add migrateuri if exists and check for default example
    if migrate_uri:
        if migrate_uri.count("EXAMPLE"):
            raise error.TestNAError("Set up the migrate_uri.")
        extra = ("%s --migrateuri=%s" % (extra, migrate_uri))

    # To migrate you need to have a shared disk between hosts
    if shared_storage.count("EXAMPLE"):
        raise error.TestError("For migration you need to have a shared "
                              "storage.")

    # Get expected cache state for test
    attach_scsi_disk = "yes" == params.get("attach_scsi_disk", "no")
    disk_cache = params.get("virsh_migrate_disk_cache", "none")
    unsafe_test = False
    if options.count("unsafe") and disk_cache != "none":
        unsafe_test = True

    exception = False
    try:
        # Change the disk of the vm to shared disk
        if vm.is_alive():
            vm.destroy(gracefully=False)

        devices = vm.get_blk_devices()
        for device in devices:
            s_detach = virsh.detach_disk(vm_name, device, "--config", debug=True)
            if not s_detach:
                logging.error("Detach vda failed before test.")

        subdriver = utils_test.get_image_info(shared_storage)['format']
        extra_attach = ("--config --driver qemu --subdriver %s --cache %s"
                        % (subdriver, disk_cache))
        s_attach = virsh.attach_disk(vm_name, shared_storage, "vda",
                                     extra_attach, debug=True)
        if s_attach.exit_status != 0:
            logging.error("Attach vda failed before test.")

        # Attach a scsi device for special testcases
        if attach_scsi_disk:
            shared_dir = os.path.dirname(shared_storage)
            scsi_disk = "%s/scsi_test.img" % shared_dir
            utils.run("qemu-img create -f qcow2 %s 100M" % scsi_disk)
            s_attach = virsh.attach_disk(vm_name, scsi_disk, "sdb",
                                         extra_attach, debug=True)
            if s_attach.exit_status != 0:
                logging.error("Attach another scsi disk failed.")

        vm.start()
        vm.wait_for_login()

        # Confirm VM can be accessed through network.
        time.sleep(delay)
        vm_ip = vm.get_address()
        s_ping, o_ping = utils_test.ping(vm_ip, count=2, timeout=delay)
        logging.info(o_ping)
        if s_ping != 0:
            raise error.TestError("%s did not respond after %d sec."
                                  % (vm.name, delay))

        # Prepare for --dname dest_exist_vm
        if extra.count("dest_exist_vm"):
            logging.debug("Preparing a new vm on destination for exist dname")
            vmxml = vm_xml.VMXML.new_from_dumpxml(vm.name)
            vmxml.vm_name = extra.split()[1].strip()
            del vmxml.uuid
            # Define a new vm on destination for --dname
            virsh.define(vmxml.xml, uri=dest_uri)

        # Prepare for --xml.
        logging.debug("Preparing new xml file for --xml option.")
        if options.count("xml") or extra.count("xml"):
            dest_xmlfile = params.get("virsh_migrate_xml", "")
            if dest_xmlfile:
                ret_attach = vm.attach_interface("--type bridge --source "
                                                 "virbr0 --target tmp-vnet",
                                                 True, True)
                if not ret_attach:
                    exception = True
                    raise error.TestError("Attaching nic to %s failed."
                                          % vm.name)
                ifaces = vm_xml.VMXML.get_net_dev(vm.name)
                new_nic_mac = vm.get_virsh_mac_address(ifaces.index("tmp-vnet"))
                vm_xml_new = vm.get_xml()
                logging.debug("Xml file on source: %s" % vm_xml_new)
                f = codecs.open(dest_xmlfile, 'wb', encoding='utf-8')
                f.write(vm_xml_new)
                f.close()
                if not os.path.exists(dest_xmlfile):
                    exception = True
                    raise error.TestError("Creating %s failed." % dest_xmlfile)

        # Turn VM into certain state.
        logging.debug("Turning %s into certain state." % vm.name)
        if src_state == "paused":
            if vm.is_alive():
                vm.pause()
        elif src_state == "shut off":
            if vm.is_alive():
                if not vm.shutdown():
                    vm.destroy()

        # Turn libvirtd into certain state.
        logging.debug("Turning libvirtd into certain status.")
        if libvirtd_state == "off":
            utils_libvirtd.libvirtd_stop()

        # Test uni-direction migration.
        logging.debug("Doing migration test.")
        if vm_ref != vm_name:
            vm.name = vm_ref    # For vm name error testing.
        if unsafe_test:
            options = "--live"
        ret_migrate = do_migration(delay, vm, dest_uri, options, extra)

        # Check unsafe result and may do migration again in right mode
        check_unsafe_result = True
        if ret_migrate is False and unsafe_test:
            options = params.get("virsh_migrate_options")
            ret_migrate = do_migration(delay, vm, dest_uri, options, extra)
        elif ret_migrate and unsafe_test:
            check_unsafe_result = False
        if vm_ref != vm_name:
            vm.name = vm_name

        # Recover libvirtd state.
        logging.debug("Recovering libvirtd status.")
        if libvirtd_state == "off":
            utils_libvirtd.libvirtd_start()

        # Check vm state on destination.
        logging.debug("Checking %s state on %s." % (vm.name, vm.connect_uri))
        if options.count("dname") or extra.count("dname"):
            vm.name = extra.split()[1].strip()
        check_dest_state = True
        dest_state = params.get("virsh_migrate_dest_state", "running")
        check_dest_state = check_vm_state(vm, dest_state)
        logging.info("Supposed state: %s" % dest_state)
        logging.info("Actual state: %s" % vm.state())

        # Recover VM state.
        logging.debug("Recovering %s state." % vm.name)
        if src_state == "paused":
            vm.resume()
        elif src_state == "shut off":
            vm.start()

        # Checking for --persistent.
        logging.debug("Checking for --persistent option.")
        check_dest_persistent = True
        if options.count("persistent") or extra.count("persistent"):
            if not vm.is_persistent():
                check_dest_persistent = False

        # Checking for --undefinesource.
        logging.debug("Checking for --undefinesource option.")
        check_src_undefine = True
        if options.count("undefinesource") or extra.count("undefinesource"):
            logging.info("Verifying <virsh domstate> DOES return an error."
                         "%s should not exist on %s." % (vm_name, src_uri))
            if virsh.domain_exists(vm_name, uri=src_uri):
                check_src_undefine = False

        # Checking for --dname.
        logging.debug("Checking for --dname option.")
        check_dest_dname = True
        if options.count("dname") or extra.count("dname"):
            dname = extra.split()[1].strip()
            if not virsh.domain_exists(dname, uri=dest_uri):
                check_dest_dname = False

        # Checking for --xml.
        logging.debug("Checking for --xml option.")
        check_dest_xml = True
        if options.count("xml") or extra.count("xml"):
            if dest_xmlfile:
                vm_dest_xml = vm.get_xml()
                logging.info("Xml file on destination: %s" % vm_dest_xml)
                if not re.search(new_nic_mac, vm_dest_xml):
                    check_dest_xml = False

        # Repeat the migration from destination to source.
        if params.get("virsh_migrate_back", "no") == 'yes':
            back_dest_uri = params.get("virsh_migrate_back_desturi", 'default')
            back_options = params.get("virsh_migrate_back_options", 'default')
            back_extra = params.get("virsh_migrate_back_extra", 'default')
            if back_dest_uri == 'default':
                back_dest_uri = src_uri
            if back_options == 'default':
                back_options = options
            if back_extra == 'default':
                back_extra = extra
            ret_migrate = do_migration(
                delay, vm, back_dest_uri, back_options, back_extra)

    except Exception, detail:
        exception = True
        logging.error("%s: %s" % (detail.__class__, detail))

    # Whatever error occurs, we have to clean up all environment.
    # Make sure vm.connect_uri is the destination uri.
    vm.connect_uri = dest_uri
    if options.count("dname") or extra.count("dname"):
        # Use the VM object to remove
        vm.name = extra.split()[1].strip()
        cleanup_dest(vm, src_uri)
        vm.name = vm_name
    else:
        cleanup_dest(vm, src_uri)

    # Recover source (just in case).
    # Simple sync cannot be used here, because the vm may not exists and
    # it cause the sync to fail during the internal backup.
    vm.destroy()
    vm.undefine()
    orig_config_xml.define()

    # Cleanup source.
    if os.path.exists(dest_xmlfile):
        os.remove(dest_xmlfile)

    if attach_scsi_disk:
        utils.run("rm -f %s" % scsi_disk, ignore_status=True)

    if exception:
        raise error.TestError(
            "Error occurred. \n%s: %s" % (detail.__class__, detail))

    # Check test result.
    if status_error == 'yes':
        if ret_migrate:
            raise error.TestFail("Migration finished with unexpected status.")
    else:
        if not ret_migrate:
            raise error.TestFail("Migration finished with unexpected status.")
        if not check_dest_state:
            raise error.TestFail("Wrong VM state on destination.")
        if not check_dest_persistent:
            raise error.TestFail("VM is not persistent on destination.")
        if not check_src_undefine:
            raise error.TestFail("VM is not undefined on source.")
        if not check_dest_dname:
            raise error.TestFail("Wrong VM name %s on destination." % dname)
        if not check_dest_xml:
            raise error.TestFail("Wrong xml configuration on destination.")
        if not check_unsafe_result:
            raise error.TestFail("Migration finished in unsafe mode.")
