# Copyright (c) 2016 Ultimaker B.V.
# Cura is released under the terms of the AGPLv3 or higher.

from PyQt5.QtCore import QObject, pyqtSlot, pyqtProperty, pyqtSignal
import UM.Settings
from UM.Application import Application
import cura.Settings
from UM.Logger import Logger


##    The settingInheritance manager is responsible for checking each setting in order to see if one of the "deeper"
#     containers has a setting function and the topmost one with a value has a value. We need to have this check
#     because some profiles tend to have 'hardcoded' values that break our inheritance. A good example of that are the
#     speed settings. If all the children of print_speed have a single value override, changing the speed won't
#     actually do anything, as only the 'leaf' settings are used by the engine.
class SettingInheritanceManager(QObject):
    def __init__(self, parent = None):
        super().__init__(parent)
        Application.getInstance().globalContainerStackChanged.connect(self._onGlobalContainerChanged)
        self._global_container_stack = None
        self._settings_with_inheritance_warning = []
        self._active_container_stack = None
        self._onGlobalContainerChanged()

        cura.Settings.ExtruderManager.getInstance().activeExtruderChanged.connect(self._onActiveExtruderChanged)
        self._onActiveExtruderChanged()

    settingsWithIntheritanceChanged = pyqtSignal()

    ##  Get the keys of all children settings with an override.
    @pyqtSlot(str, result = "QStringList")
    def getChildrenKeysWithOverride(self, key):
        definitions = self._global_container_stack.getBottom().findDefinitions(key=key)
        if not definitions:
            Logger.log("w", "Could not find definition for key [%s]", key)
            return []
        result = []
        for key in definitions[0].getAllKeys():
            if key in self._settings_with_inheritance_warning:
                result.append(key)
        return result

    @pyqtSlot(str, str, result = "QStringList")
    def getOverridesForExtruder(self, key, extruder_index):
        multi_extrusion = self._global_container_stack.getProperty("machine_extruder_count", "value") > 1
        if not multi_extrusion:
            return self._settings_with_inheritance_warning
        extruder = cura.Settings.ExtruderManager.getInstance().getExtruderStack(extruder_index)
        if not extruder:
            Logger.log("w", "Unable to find extruder for current machine with index %s", extruder_index)
            return []

        definitions = self._global_container_stack.getBottom().findDefinitions(key=key)
        if not definitions:
            Logger.log("w", "Could not find definition for key [%s] (2)", key)
            return []
        result = []
        for key in definitions[0].getAllKeys():
            if self._settingIsOverwritingInheritance(key, extruder):
                result.append(key)

        return result

    @pyqtSlot(str)
    def manualRemoveOverride(self, key):
        if key in self._settings_with_inheritance_warning:
            self._settings_with_inheritance_warning.remove(key)
            self.settingsWithIntheritanceChanged.emit()

    @pyqtSlot()
    def forceUpdate(self):
        self._update()

    def _onActiveExtruderChanged(self):
        new_active_stack = cura.Settings.ExtruderManager.getInstance().getActiveExtruderStack()
        if not new_active_stack:
            new_active_stack = self._global_container_stack

        if new_active_stack != self._active_container_stack:  # Check if changed
            if self._active_container_stack:  # Disconnect signal from old container (if any)
                self._active_container_stack.propertyChanged.disconnect(self._onPropertyChanged)
                self._active_container_stack.containersChanged.disconnect(self._onContainersChanged)

            self._active_container_stack = new_active_stack
            self._active_container_stack.propertyChanged.connect(self._onPropertyChanged)
            self._active_container_stack.containersChanged.connect(self._onContainersChanged)
            self._update()  # Ensure that the settings_with_inheritance_warning list is populated.

    def _onPropertyChanged(self, key, property_name):
        if (property_name == "value" or property_name == "enabled") and self._global_container_stack:
            definitions = self._global_container_stack.getBottom().findDefinitions(key = key)
            if not definitions:
                return

            has_overwritten_inheritance = self._settingIsOverwritingInheritance(key)

            settings_with_inheritance_warning_changed = False

            # Check if the setting needs to be in the list.
            if key not in self._settings_with_inheritance_warning and has_overwritten_inheritance:
                self._settings_with_inheritance_warning.append(key)
                settings_with_inheritance_warning_changed = True
            elif key in self._settings_with_inheritance_warning and not has_overwritten_inheritance:
                self._settings_with_inheritance_warning.remove(key)
                settings_with_inheritance_warning_changed = True

            # Find the topmost parent (Assumed to be a category)
            parent = definitions[0].parent
            while parent.parent is not None:
                parent = parent.parent

            if parent.key not in self._settings_with_inheritance_warning and has_overwritten_inheritance:
                # Category was not in the list yet, so needs to be added now.
                self._settings_with_inheritance_warning.append(parent.key)
                settings_with_inheritance_warning_changed = True

            elif parent.key in self._settings_with_inheritance_warning and not has_overwritten_inheritance:
                # Category was in the list and one of it's settings is not overwritten.
                if not self._recursiveCheck(parent):  # Check if any of it's children have overwritten inheritance.
                    self._settings_with_inheritance_warning.remove(parent.key)
                    settings_with_inheritance_warning_changed = True

            # Emit the signal if there was any change to the list.
            if settings_with_inheritance_warning_changed:
                self.settingsWithIntheritanceChanged.emit()

    def _recursiveCheck(self, definition):
        for child in definition.children:
            if child.key in self._settings_with_inheritance_warning:
                return True
            if child.children:
                if self._recursiveCheck(child):
                    return True
        return False

    @pyqtProperty("QVariantList", notify = settingsWithIntheritanceChanged)
    def settingsWithInheritanceWarning(self):
        return self._settings_with_inheritance_warning

    ##  Check if a setting has an inheritance function that is overwritten
    def _settingIsOverwritingInheritance(self, key, stack = None):
        has_setting_function = False
        if not stack:
            stack = self._active_container_stack
        containers = []

        ## Check if the setting has a user state. If not, it is never overwritten.
        has_user_state = stack.getProperty(key, "state") == UM.Settings.InstanceState.User
        if not has_user_state:
            return False

        ## If a setting is not enabled, don't label it as overwritten (It's never visible anyway).
        if not stack.getProperty(key, "enabled"):
            return False

        ## Also check if the top container is not a setting function (this happens if the inheritance is restored).
        if isinstance(stack.getTop().getProperty(key, "value"), UM.Settings.SettingFunction):
            return False

        ##  Mash all containers for all the stacks together.
        while stack:
            containers.extend(stack.getContainers())
            stack = stack.getNextStack()
        has_non_function_value = False
        for container in containers:
            try:
                value = container.getProperty(key, "value")
            except AttributeError:
                continue
            if value is not None:
                # If a setting doesn't use any keys, it won't change it's value, so treat it as if it's a fixed value
                has_setting_function = isinstance(value, UM.Settings.SettingFunction)
                if has_setting_function:
                    for setting_key in value.getUsedSettingKeys():
                        if setting_key in self._active_container_stack.getAllKeys():
                            break # We found an actual setting. So has_setting_function can remain true
                    else:
                        # All of the setting_keys turned out to not be setting keys at all!
                        # This can happen due enum keys also being marked as settings.
                        has_setting_function = False

                if has_setting_function is False:
                    has_non_function_value = True
                    continue

            if has_setting_function:
                break  # There is a setting function somewhere, stop looking deeper.
        return has_setting_function and has_non_function_value

    def _update(self):
        self._settings_with_inheritance_warning = []  # Reset previous data.

        # Check all setting keys that we know of and see if they are overridden.
        for setting_key in self._global_container_stack.getAllKeys():
            override = self._settingIsOverwritingInheritance(setting_key)
            if override:
                self._settings_with_inheritance_warning.append(setting_key)

        # Check all the categories if any of their children have their inheritance overwritten.
        for category in self._global_container_stack.getBottom().findDefinitions(type = "category"):
            if self._recursiveCheck(category):
                self._settings_with_inheritance_warning.append(category.key)

        # Notify others that things have changed.
        self.settingsWithIntheritanceChanged.emit()

    def _onGlobalContainerChanged(self):
        if self._global_container_stack:
            self._global_container_stack.propertyChanged.disconnect(self._onPropertyChanged)
            self._global_container_stack.containersChanged.disconnect(self._onContainersChanged)
        self._global_container_stack = Application.getInstance().getGlobalContainerStack()
        if self._global_container_stack:
            self._global_container_stack.containersChanged.connect(self._onContainersChanged)
            self._global_container_stack.propertyChanged.connect(self._onPropertyChanged)
        self._onActiveExtruderChanged()

    def _onContainersChanged(self, container):
        # TODO: Multiple container changes in sequence now cause quite a few recalculations.
        # This isn't that big of an issue, but it could be in the future.
        self._update()

    @staticmethod
    def createSettingInheritanceManager(engine=None, script_engine=None):
        return SettingInheritanceManager()