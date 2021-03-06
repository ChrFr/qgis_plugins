# -*- coding: utf-8 -*-
"""
/***************************************************************************
 SHKPluginDialog
                                 A QGIS plugin
 Plugin zur Berechnung von Erreichbarkeiten im Saale-Holzland-Kreis

                             -------------------
        begin                : 2017-07-20
        git sha              : $Format:%H$
        copyright            : (C) 2017 by GGR
        email                : franke@ggr-planung.de
 ***************************************************************************/

/***************************************************************************
 *                                                                         *
 *   This program is free software; you can redistribute it and/or modify  *
 *   it under the terms of the GNU General Public License as published by  *
 *   the Free Software Foundation; either version 2 of the License, or     *
 *   (at your option) any later version.                                   *
 *                                                                         *
 ***************************************************************************/
"""

import os
import sys
import subprocess
import traceback
from PyQt4 import QtGui, uic, QtCore
from PyQt4.QtXml import QDomDocument
from osgeo import gdal
from time import time, sleep
import re
import json
from qgis.core import (QgsDataSourceURI, QgsVectorLayer, 
                       QgsMapLayerRegistry, QgsRasterLayer,
                       QgsProject, QgsLayerTreeLayer, QgsRectangle,
                       QgsVectorFileWriter, QgsComposition, QgsLegendRenderer,
                       QgsComposerLegendStyle, QgsLayerTreeGroup)
from qgis.gui import QgsLayerTreeMapCanvasBridge
from qgis.utils import iface
import numpy as np
from collections import defaultdict
import pickle
from filter_tree import FilterTree

FORM_CLASS, _ = uic.loadUiType(os.path.join(
    os.path.dirname(__file__), 'shk_plugin_dialog_base.ui'))

from config import Config
from connection import DBConnection, Login
from queries import (get_values, update_erreichbarkeiten,
                     update_gemeinde_erreichbarkeiten, clone_scenario,
                     get_scenarios, remove_scenario)
from ui_elements import (SimpleSymbology, SimpleFillSymbology, 
                         GraduatedSymbology, WaitDialog,
                         EXCEL_FILTER, KML_FILTER, PDF_FILTER,
                         browse_file, browse_folder, CreateScenarioDialog,
                         HelpDialog, ExportPDFDialog)

config = Config()

SCHEMA = 'einrichtungen'

basepath = os.path.split(__file__)[0]

# file containing structure of filter-tree
FILTER_XML = os.path.join(os.path.split(__file__)[0], "filter.xml")
# background map definitions for google maps-layer
GOOGLE_XML = os.path.join(basepath, 'google_maps.xml')
# contains help-texts when clicking on question-marks in UI
HELP_FILE = os.path.join(basepath, 'help.txt')
# composer-template for exporting to pdf
REPORT_TEMPLATE_PATH = os.path.join(basepath, 'report_template.qpt')


class SHKPluginDialog(QtGui.QMainWindow, FORM_CLASS):
    def __init__(self, parent=None):
        """Constructor."""
        super(SHKPluginDialog, self).__init__(parent)
        # Set up the user interface from Designer.
        # After setupUI you can access any designer object by doing
        # self.<objectname>, and you can use autoconnect slots - see
        # http://qt-project.org/doc/qt-4.8/designer-using-a-ui-file.html
        # #widgets-and-dialogs-with-auto-connect
        self.setupUi(self)
        self.load_config()
        self.save_button.clicked.connect(self.save_config)
        self.browse_cache_button.clicked.connect(self.browse_cache)
        self.connect_button.clicked.connect(self.connect)
        self.login = None

        # colors of reachability layers
        self.reach_color_ranges =  [
            (0, 5, 'unter 5 Minuten', QtGui.QColor(37, 52, 148)), 
            (5, 10, '10 bis 15 Minuten', QtGui.QColor(42, 111, 176)), 
            (10, 15, '15 bis 20 Minuten', QtGui.QColor(56, 160, 191)), 
            (15, 20, '20 bis 25 Minuten', QtGui.QColor(103, 196, 189)), 
            (20, 30, '25 bis 30 Minuten', QtGui.QColor(179, 225, 184)), 
            (30, 60, '30 bis 60 Minuten', QtGui.QColor(255, 212, 184)), 
            (60, 120, '60 bis 120 Minuten', QtGui.QColor(251, 154, 153)), 
            (120, 99999999, 'mehr als 120 Minuten', QtGui.QColor(227, 88, 88)), 
        ]
        
        # tags internally used for the the institutions
        self.ein_tags = {
            'Bildungseinrichtungen': 'bildung',
            'Gesundheit und Feuerwehr': 'gesundheit',
            'Nahversorgung': 'nahversorgung'
        }
        
        # colors of institutions in map
        self.colors = {
            'Bildungseinrichtungen': 'orange',
            'Gesundheit und Feuerwehr': 'red',
            'Nahversorgung': '#F781F3'
        }
        
        # borders in map
        self.borders = {
            'Gemeinden': QtCore.Qt.DotLine,
            'Verwaltungsgemeinschaften': QtCore.Qt.DashLine, 
            'Kreise': QtCore.Qt.SolidLine
        }
        
        # filter buttons
        for button in ['filter_button', 'filter_button_2', 'filter_button_3']:
            getattr(self, button).clicked.connect(self.apply_filters)
        
        self.filter_selection_button.clicked.connect(self.filter_selection)
    
        # reachability buttons
        self.calculate_car_button.clicked.connect(self.calculate_car)
        self.calculate_ov_button.clicked.connect(
            lambda: self.wait_call(self.add_ov_layers))
        
        self.export_excel_button.clicked.connect(
            lambda: self.export_filter_layer(ext='xlsx'))
        self.export_kml_button.clicked.connect(
            lambda: self.export_filter_layer(ext='kml'))
        self.export_pdf_button.clicked.connect(self.export_pdf)
        
        self.canvas = iface.mapCanvas()
        
        # disable first tabs at startup (till connection)
        for i in range(2):
            self.main_tabs.setTabEnabled(i, False)
        self.car_groupbox.setEnabled(False)
        self.ov_groupbox.setEnabled(False)
        self.export_groupbox.setEnabled(False)
            
        self.active_scenario = None
        self.active_scenario_label.setText(u'kein Datensatz ausgewählt')
        self.active_scenario_label.setStyleSheet('color: red')
        self.categories = {
            'Bildungseinrichtungen': None,
            'Gesundheit und Feuerwehr': None,
            'Nahversorgung': None
        }
        
        # on change scenario-combo:
        # enable/disable buttons, fill fields with info about selected scenario
        def scenario_select(idx):
            scenario = self.scenario_combo.itemData(idx)
            if not scenario:
                name = user = date = '-'
                editable = False
                self.scen_copy_button.setEnabled(False)
            else:
                name = scenario.name
                user = scenario.user if scenario.user else '-'
                date = '{:%d.%m.%Y - %H:%M}'.format(scenario.date) if scenario.date else '-'
                editable = scenario.editable
                self.scen_copy_button.setEnabled(True)
            self.scen_select_button.setEnabled(editable)
            self.scen_delete_button.setEnabled(editable)
            self.scen_name_edit.setText(name)
            self.scen_user_edit.setText(user)
            self.scen_date_edit.setText(date)
    
        self.scenario_combo.currentIndexChanged.connect(scenario_select)
        
        # get the currently selected scenario in combo
        def get_selected_scenario(): 
            idx = self.scenario_combo.currentIndex()
            scenario = self.scenario_combo.itemData(idx)
            return scenario
        
        # scenario- related buttons
        self.scen_select_button.clicked.connect(
            lambda: self.activate_scenario(get_selected_scenario()))
        self.scen_delete_button.clicked.connect(
            lambda: self.remove_scenario(get_selected_scenario()))
        self.scen_copy_button.clicked.connect(
            lambda: self.clone_scenario(get_selected_scenario()))
        self.scen_refresh_button.clicked.connect(self.refresh_scen_list)
        
        # Help Buttons
        
        self.conn_help_button.clicked.connect(
            lambda: show_help(self, 'connection', HELP_FILE))
        self.settings_help_button.clicked.connect(
            lambda: show_help(self, 'settings', HELP_FILE))
        self.datasets_help_button.clicked.connect(
            lambda: show_help(self, 'datasets', HELP_FILE))
        self.filter_sel_help_button.clicked.connect(
            lambda: show_help(self, 'filter_selection', HELP_FILE))
        self.filter_fields_help_button.clicked.connect(
            lambda: show_help(self, 'filter', HELP_FILE))
        self.car_help_button.clicked.connect(
            lambda: show_help(self, 'reachability_car', HELP_FILE))
        self.oepnv_help_button.clicked.connect(
            lambda: show_help(self, 'reachability_oepnv', HELP_FILE))
        self.export_inst_help_button.clicked.connect(
            lambda: show_help(self, 'export', HELP_FILE))
        self.export_pdf_help_button.clicked.connect(
            lambda: show_help(self, 'export_pdf', HELP_FILE))
        self.oepnv_info_button.clicked.connect(
            lambda: show_help(self, 'oepnv_info', HELP_FILE, height=100))
        
    def init_filters(self, scenario):
        '''
        build the filter-tree for given scenario
        '''
        if not scenario:
            return
        scenario_id = scenario.id
        
        self.categories['Bildungseinrichtungen'] = FilterTree(
            'Bildungseinrichtungen', 'bildung_szenario', scenario_id,
            self.db_conn, self.schools_tree)
        self.categories['Gesundheit und Feuerwehr'] = FilterTree(
            'Gesundheit und Feuerwehr', 'gesundheit_szenario', scenario_id,
            self.db_conn, self.medicine_tree)
        self.categories['Nahversorgung'] = FilterTree(
            'Nahversorgung', 'nahversorgung_szenario', scenario_id,
            self.db_conn, self.supply_tree)

        region_node = FilterTree.region_node(self.db_conn)
        #start = time()
        for category, filter_tree in self.categories.iteritems():
            filter_tree.from_xml(FILTER_XML)  #, region_node=region_node)
        #print('Filter init {}s'.format(time() - start))
    
    def load_config(self):
        '''
        load the config from config file into the settings-form
        '''
        db_config = config.db_config
        self.user_edit.setText(str(db_config['username']))
        self.pass_edit.setText(str(db_config['password']))
        self.db_edit.setText(str(db_config['db_name']))
        self.host_edit.setText(str(db_config['host']))
        self.port_edit.setText(str(db_config['port']))
        self.cache_edit.setText(str(config.cache_folder))
        
    def save_config(self):
        '''
        save settings-form into config-file
        '''
        db_config = config.db_config
        db_config['username'] = str(self.user_edit.text())
        db_config['password'] = str(self.pass_edit.text())
        db_config['db_name'] = str(self.db_edit.text())
        db_config['host'] = str(self.host_edit.text())
        db_config['port'] = str(self.port_edit.text())
        config.cache_folder = str(self.cache_edit.text())

        config.write()

    def browse_cache(self):
        '''
        user input for cache-path
        '''
        folder = browse_folder(self.cache_edit.text(),
                               u'Verzeichnis für den Cache wählen',
                               parent=self)
        if folder:
            self.cache_edit.setText(folder)

    def connect(self):
        '''
        connect to database as stored in config
        '''
        db_config = config.db_config
        self.login = Login(host=db_config['host'], port=db_config['port'],
                           user=db_config['username'],
                           password=db_config['password'],
                           db=db_config['db_name'])
        self.db_conn = DBConnection(self.login)
        try:
            self.db_conn.fetch('SELECT * FROM pg_index')
        except:
            QtGui.QMessageBox.information(
                self, 'Fehler',
                (u'Verbindung zur Datenbank fehlgeschlagen.\n'
                u'Bitte überprüfen Sie die Einstellungen!'))
            self.login = None
            return
        #diag = WaitDialogThreaded(self.refresh, parent=self,
                          #parent_thread=iface.mainWindow())
        self.connection_label.setText('verbunden')
        self.connection_label.setStyleSheet('color: green')
        self.main_tabs.setTabEnabled(0, True)
        self.ov_groupbox.setEnabled(True)
        self.main_tabs.setCurrentIndex(0)
        self.refresh_scen_list()
        #self.wait_call(self.init_filters)
        #self.wait_call(self.init_layers)
    
    def activate_scenario(self, scenario):
        '''
        activate the given scenario, init filters and adapt ui to scenario
        '''
        if scenario and not scenario.editable:
            return
        # you may pass None as a scenario to deactivate current one
        activated = True if scenario else False
        self.wait_call(lambda: self.init_filters(scenario))
        self.wait_call(lambda: self.init_layers(scenario))
        self.active_scenario = scenario
        font = self.active_scenario_label.font()
        font.setBold(activated)
        self.active_scenario_label.setFont(font)
        # show active scenario in ui
        if activated:
            label = u'{n} {u}'.format(
                n=scenario.name,
                u=u'@{}'.format(scenario.user) if scenario.user else '')
            self.active_scenario_label.setText(label)
            self.active_scenario_label.setStyleSheet('color: black')
        else:
            self.active_scenario_label.setText(u'kein Datensatz ausgewählt')
            self.active_scenario_label.setStyleSheet('color: red')
        # activate filters and erreichbarkeiten
        for i in range(1, 2):
            self.main_tabs.setTabEnabled(i, activated)
        self.car_groupbox.setEnabled(activated)
        self.export_groupbox.setEnabled(activated)
        self.refresh_scen_list()
            
    def wait_call(self, function):
        '''
        display wait-dialog while executing given function, not threaded
        (qgis doesn't seem to handle multiple threads well)
        '''
        diag = WaitDialog(function, title='Bitte warten', parent=self)
        diag.show()
        try:
            function()
        except Exception as e:
            traceback.print_exc()
        finally:
            diag.close()
    
    def clone_scenario(self, scenario):
        '''
        make a copy of given scenario in database
        '''
        dialog = CreateScenarioDialog(parent=self)
        result = dialog.exec_()
        ok = result == QtGui.QDialog.Accepted
        if not ok:
            return
        name = dialog.name
        user = dialog.user
        self.wait_call(lambda: clone_scenario(scenario.id, name, user, self.db_conn))
        self.refresh_scen_list()
        QtGui.QMessageBox.information(self, 'Kopieren erfolgreich',
                                      u'Der Datensatz "{}" wurde angelegt '
                                      u'und kann über das Auswahlmenü '
                                      u'"Bestehende Datensätze" aktiviert werden.'
                                      .format(name))
        
    def remove_scenario(self, scenario):
        '''
        remove given scenario from database
        '''
        
        if not scenario.editable:
            return
        msg = QtGui.QMessageBox(QtGui.QMessageBox.Warning, 'Achtung',
                                u'Wollen Sie den Datensatz "{}" und '
                                u'seine Daten wirklich löschen?'
                                .format(scenario.name), parent=self)
        msg.setStandardButtons(QtGui.QMessageBox.Ok | QtGui.QMessageBox.Cancel)
        result = msg.exec_()
        ok = result == QtGui.QMessageBox.Ok
        if not ok:
            return
        remove_scenario(scenario.id, self.db_conn)
        root = QgsProject.instance().layerTreeRoot()
        scen_to_delete = [child for child in root.children()
                          if (isinstance(child, QgsLayerTreeGroup)
                          and child.name() == scenario.name)]
        for match in scen_to_delete:
            root.removeChildNode(match)
        self.refresh_scen_list()
        if self.active_scenario and scenario.id == self.active_scenario.id:
            self.activate_scenario(None)
        QtGui.QMessageBox.information(self, u'Löschen erfolgreich',
                                      u'Der Datensatz "{}" wurde gelöscht. '
                                      .format(scenario.name))
    
    def refresh_scen_list(self):
        '''
        refresh the combobox holding available scenarios
        '''
        scenarios = get_scenarios(self.db_conn)
        self.scenario_combo.clear()
        select = idx = 0
        for scenario in scenarios:
            label = u'{n} {u}'.format(
                n=scenario.name,
                u=u'@{}'.format(scenario.user) if scenario.user else '')
            if not scenario.editable:
                label += ' (nur kopierbar!)'
            if self.active_scenario and self.active_scenario.id == scenario.id:
                select = idx
                label += ' (aktiv)'
            self.scenario_combo.addItem(label, scenario)
            idx += 1
        self.scenario_combo.setCurrentIndex(select)

    def add_db_layer(self, name, schema, tablename, geom,
                     symbology=None, uri=None, key=None, zoom=False,
                     group=None, where='', visible=True, to_shape=False,
                     hide_in_composer=False):
        """
        add a layer with database as source to the qgis-layer-window
        
        Parameters
        ----------
        name: layer-label that will be displayed in layer-window
        schema: name of database schema
        tablename: name of table in schema
        where: optional, sql-where-clause to limit the query-response
        geom: name of the geometry-column to use to display on map
        key: optional, primary key
        visible: optional, if True, added layer will be unchecked in layer-window
        uri: optional, specific uri to source (qgis-style), if not given it will
                       be created from login and database info
        zoom: optional, zoom to layer after adding, if True
        to_shape: optional, if True, store the database-layer to a file in the cache-
                            folder, adds the link to this file to layer-window
                            instead of direct link to database
        group: optional, name of the group to add the layer to, if not given
                         layer is added to root
        hide_in_composer: optional, if True, the layer will not be displayed in
                                    the legend when exporting to pdf
        """
        if not uri:
            uri = QgsDataSourceURI()
            uri.setConnection(self.login.host,
                              self.login.port,
                              self.login.db,
                              self.login.user,
                              self.login.password)
            uri.setDataSource(schema, tablename, geom, aKeyColumn=key,
                              aSql=where)
            uri = uri.uri(False)
        layer = QgsVectorLayer(uri, name, "postgres")
        remove_layer(name, group)
        if to_shape:
            path = config.cache_folder
            if not os.path.exists(path):
                os.mkdir(path)
            fn = os.path.join(path, u'{}_{}.shp'.format(
                self.active_scenario.name, name))
            QgsVectorFileWriter.writeAsVectorFormat(
                layer, fn, 'utf-8', None, 'ESRI Shapefile')
            layer = QgsVectorLayer(fn, name, "ogr")
        # if no group is given, add to layer-root
        QgsMapLayerRegistry.instance().addMapLayer(layer, group is None)
        #if where:
            #layer.setSubsetString(where)
        treelayer = None
        if group:
            treelayer = group.addLayer(layer)
        if symbology:
            symbology.apply(layer)
        if zoom:
            extent = layer.extent()
            self.canvas.setExtent(extent)
        iface.legendInterface().setLayerVisible(layer, visible)
        self.canvas.refresh()
        if hide_in_composer and treelayer:
            QgsLegendRenderer.setNodeLegendStyle(
                treelayer, QgsComposerLegendStyle.Hidden)
        return layer
        
    def add_xml_background_map(self, xml, layer_name = 'GoogleMaps',
                               group=None, visible=True):
        '''
        add background map from xml file to given group (or root)
        '''
        for child in group.children():
            pass
        layer = QgsRasterLayer(xml, layer_name)
    
        #layer = QgsRasterLayer("http://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer?f=json&pretty=true", "layer")
        remove_layer(layer_name, group)
        QgsMapLayerRegistry.instance().addMapLayer(layer, group is None)
        if group:
            treelayer = group.addLayer(layer)
            QgsLegendRenderer.setNodeLegendStyle(
                treelayer, QgsComposerLegendStyle.Hidden)
        iface.legendInterface().setLayerVisible(layer, visible)
    
    def add_wms_background_map(self, group=None):
        '''
        add b/w terrestris-wms-layer to given group (or root)
        '''
        layer_name = 'OpenStreetMap WMS - by terrestris'
        remove_layer(layer_name, group)
        url = ('crs=EPSG:31468&dpiMode=7&format=image/png&layers=OSM-WMS&'
               'styles=&url=http://ows.terrestris.de/osm-gray/service')
        layer = QgsRasterLayer(url, layer_name, 'wms')
        QgsMapLayerRegistry.instance().addMapLayer(layer, group is None)
        if group:
            treelayer = group.addLayer(layer)
            QgsLegendRenderer.setNodeLegendStyle(
                treelayer, QgsComposerLegendStyle.Hidden)

    def init_layers(self, scenario):
        '''
        initialize the layers in layer-window for given scenario
        including adding groups and background-layers and setting the
        editability of the institutional layers
        '''
        if not scenario:
            return
        scen_group = get_group(scenario.name, add_at_index=0)
        # just for the right initial order
        get_group('Filter', scen_group)
        cat_group = get_group('Einrichtungen', scen_group)
        get_group('Erreichbarkeiten PKW', scen_group)
        get_group(u'Erreichbarkeiten ÖPNV')
        border_group = get_group('Verwaltungsgrenzen')
        self.add_wms_background_map(group=get_group('Hintergrundkarte',
                                                    add_at_index=-1))
        self.add_xml_background_map(GOOGLE_XML,
                                    group=get_group('Hintergrundkarte'),
                                    visible=False)
        
        for name, tablename in [('Gemeinden', 'gemeinden_20161231'),
                                ('Verwaltungsgemeinschaften', 'vwg_20161231'),
                                ('Kreise', 'kreis_20161231')]:
            border_style = self.borders[name]
            symbology = SimpleFillSymbology(border_style=border_style)
            self.add_db_layer(name, 'verwaltungsgrenzen', tablename,
                              'geom', group=border_group, visible=False,
                              symbology=symbology)
        
        self.canvas.refresh()
        
        ### SET THE EDITABILITY OF INSTITUTIONAL LAYER-FIELDS###
    
        columns = ['spalte', 'editierbar', 'nur_auswahl_zulassen',
                   'auswahlmoeglichkeiten', 'alias', 'auto_vervollst',
                   'typ', 'min', 'max']
        for category, filter_tree in self.categories.iteritems():
            table = filter_tree.tablename
            symbology = SimpleSymbology(self.colors[category])
            layer = self.add_db_layer(category, SCHEMA, table, 'geom_gk',
                                      symbology, group=cat_group, zoom=False,
                                      where='szenario_id={}'.format(scenario.id))
            rows = get_values('editierbare_spalten', columns,
                              self.db_conn, schema='einrichtungen',
                              where="tabelle='{}'".format(table),
                              order_by='reihenfolge')
            editable_columns = [r.spalte for r in rows]
            #if not rows:
                #continue
            for i, f in enumerate(layer.fields()):
                if f.name() == 'szenario_id':
                    layer.setEditorWidgetV2(i, 'Hidden')
                    layer.setDefaultValueExpression(i, str(scenario.id))
                    continue
                try:
                    idx = editable_columns.index(f.name())
                except:
                    layer.setEditorWidgetV2(i, 'Hidden')
                    continue
                col, is_ed, is_sel, selections, alias, auto_complete, typ, min_value, max_value = rows[idx]
                if alias:
                    layer.addAttributeAlias(i, alias) 
                if not is_ed:
                    layer.setEditorWidgetV2(i, 'Hidden')
                    continue
                # type range (integers)
                if typ == 'range':
                    layer.setEditorWidgetV2(i, 'Range')
                    layer.setEditorWidgetV2Config(
                        i, {'AllowNull': False,
                            'Min': min_value, 'Max': max_value})
                # auto complete: take all existing unique values of field,
                # text will can be auto completed to one of those in UI
                if auto_complete:
                    layer.setEditorWidgetV2(i, 'UniqueValues')
                    layer.setEditorWidgetV2Config(i, {u'Editable': True})
                # selectable values are predefined in database
                elif is_sel and selections:
                    layer.setEditorWidgetV2(i, 'ValueMap')
                    sel = []
                    for s in selections:
                        try:
                            s = s.decode('utf-8')
                        except:
                            pass
                        sel.append(s)
                    d = dict([(s, s) for s in sel])
                    layer.setEditorWidgetV2Config(i, d)
                elif is_sel:
                    layer.setEditorWidgetV2(i, 'UniqueValues')
        # zoom to extent
        extent = QgsRectangle()
        extent.setMinimal()
        for child in cat_group.children():
            if isinstance(child, QgsLayerTreeLayer):
                #print child.layer().extent()
                extent.combineExtentWith(child.layer().extent())
        self.canvas.setExtent(extent)
        self.canvas.refresh()
    
    def filter_selection(self):
        '''
        filter the active layer by selected objects
        '''
        if not self.login:
            return
        # either name of layer or group have to match a category
        active_layer = iface.activeLayer()
        categories = self.categories.keys()
        layer_error = (u'Sie müssen im Layerfenster einen '
                       u'Layer auswählen, wahlweise aus den Gruppen '
                       u'Einrichtungen oder Filter.')
        if not active_layer:
            QtGui.QMessageBox.information(self, 'Fehler', layer_error)
            return
        else:
            layer_name = active_layer.name()
            if layer_name in categories:
                category = layer_name
            else:
                project_tree = QgsProject.instance().layerTreeRoot()
                layer_item = project_tree.findLayer(active_layer.id())
                group = layer_item.parent()
                group_name = group.name()
                if group_name in categories:
                    category = group_name
                else:
                    QtGui.QMessageBox.information(self, 'Fehler', layer_error)
                    return
            selected_feats = active_layer.selectedFeatures()
            if not selected_feats:
                msg = (u'Im ausgewählten Layer {} sind keine '
                       u'Einrichtungen selektiert.'.format(layer_name))
                QtGui.QMessageBox.information(self, 'Fehler', msg)
                return
    
            parent_group = get_group('Filter')
            subgroup = get_group(category, parent_group)
            ids = [str(f.attribute('sel_id')) for f in selected_feats]
            name, ok = QtGui.QInputDialog.getText(
                self, 'Filter', 'Name des zu erstellenden Layers',
                text=get_unique_layer_name(category, subgroup))
            if not ok:
                return
            
            subset = 'sel_id in ({})'.format(','.join(ids))
            layer = QgsVectorLayer(active_layer.source(), name, "postgres")
            remove_layer(name, subgroup)
            
            QgsMapLayerRegistry.instance().addMapLayer(layer, False)
            subgroup.addLayer(layer)
            layer.setSubsetString(subset)
            symbology = SimpleSymbology(self.colors[category], shape='triangle')
            symbology.apply(layer)
            self.copy_editor_attrs(active_layer, layer)
    
    def copy_editor_attrs(self, origin_layer, destination_layer):
        '''important: both layers have to have identical fields in same order!'''

        for i, f in enumerate(origin_layer.fields()):
            destination_layer.setEditorWidgetV2(i, origin_layer.editorWidgetV2(i))
            destination_layer.setDefaultValueExpression(i, origin_layer.defaultValueExpression(i))
            destination_layer.setEditorWidgetV2Config(i, origin_layer.editorWidgetV2Config(i))
            destination_layer.addAttributeAlias(i, origin_layer.attributeAlias(i))
    
    def apply_filters(self):
        '''
        filter a layer with settings made by user in filter-tree
        '''
        if not self.login:
            return
        category = self.get_selected_tab()
        scenario_group = get_group(self.active_scenario.name)
        err_group = get_group('Einrichtungen', scenario_group)
        filter_group = get_group('Filter', scenario_group)
        subgroup = get_group(category, filter_group, hide_in_composer=False)
        name, ok = QtGui.QInputDialog.getText(
            self, 'Filter', 'Name des zu erstellenden Layers',
            text=get_unique_layer_name(category, subgroup))
        if not ok or not name:
            return
        orig_layer = None
        for child in err_group.children():
            if child.name() == category:
                layer_id = child.layerId()
                orig_layer = QgsMapLayerRegistry.instance().mapLayers()[layer_id]
                break
        
        filter_tree = self.categories[category]
        subset = filter_tree.to_sql_query(self.active_scenario.id,
                                          year=filter_tree.year_slider.value())
        matches = QgsMapLayerRegistry.instance().mapLayersByName(category)
    
        remove_layer(name, subgroup)

        layer = QgsVectorLayer(orig_layer.source(), name, "postgres")
        QgsMapLayerRegistry.instance().addMapLayer(layer, False)
        subgroup.addLayer(layer)
        layer.setSubsetString(subset)
        symbology = SimpleSymbology(self.colors[category], shape='triangle')
        symbology.apply(layer)
        self.copy_editor_attrs(orig_layer, layer)
    
    def get_filterlayer(self):
        '''
        user-input for selecting a filter-layer
        layer will be returned, if user clicks ok, else None
        '''
        items = []
        scenario_group = get_group(self.active_scenario.name)
        filter_group = get_group('Filter', scenario_group)
        for category in self.categories.iterkeys():
            subgroup = get_group(category, filter_group)
            subitems = [(category, c.layer().name())
                        for c in subgroup.children()]
            items += subitems
        if not items:
            QtGui.QMessageBox.information(
                self, 'Fehler', 'Es sind keine gefilterten Layer vorhanden.')
            return 
        item_texts = [u'{} - {}'.format(l, c) for l, c in items]
        sel, ok = QtGui.QInputDialog.getItem(self, 'Erreichbarkeiten',
                                             u'Gefilterten Layer auswählen',
                                             item_texts, 0, False)
        if not ok:
            return
        category, layer_name = items[item_texts.index(sel)]
        subgroup = get_group(category, filter_group)
        for child in subgroup.children():
            if child.layer().name() == layer_name:
                return category, child.layer()
        return
    
    def calculate_car(self):
        '''
        calculate and display the reachability by car
        '''
        if not self.login:
            return
        res = self.get_filterlayer()
        if not res:
            return
        category, layer = res
        #scenario_id = self.get_scenario_id(layer)
        #if not scenario_id:
            #return
        
        def run():
            query = layer.subsetString()
            
            tag = self.ein_tags[category]
            scenario_group = get_group(self.active_scenario.name)
            results_group = get_group('Erreichbarkeiten PKW', scenario_group,
                                      hide_in_composer=False)
            layer_name = layer.name()
            subgroup = get_group(category, results_group)
            remove_layer(layer_name, subgroup)
            err_table = 'matview_err_' + tag
            gem_table = 'erreichbarkeiten_gemeinden_' + tag
            update_erreichbarkeiten(tag, self.db_conn, self.active_scenario.id,
                                    where=query)
            update_gemeinde_erreichbarkeiten(tag, self.db_conn)
            
            symbology = GraduatedSymbology('minuten', self.reach_color_ranges,
                                           no_pen=True)
            self.add_db_layer(layer_name, 'erreichbarkeiten',
                              err_table, 'geom', key='grid_id',
                              symbology=symbology, group=subgroup,
                              zoom=False, to_shape=True)
            
            symbology = GraduatedSymbology('minuten_mittelwert'[:10],  # Shapefiles: max field-name length = 10
                                           self.reach_color_ranges,
                                           no_pen=True)
            layer_name += ' Gemeindeebene'
            self.add_db_layer(layer_name, 'erreichbarkeiten',
                              gem_table, 'geom', key='ags',
                              symbology=symbology, group=subgroup, zoom=False,
                              to_shape=True)
            
        self.wait_call(run)

    def add_ov_layers(self):
        '''
        display the reachability by OEPNV and the central places
        '''
        if not self.login:
            return
        results_group = get_group(u'Erreichbarkeiten ÖPNV')
        symbology = SimpleSymbology('yellow', shape='diamond')
        self.add_db_layer('Zentrale Orte', 'erreichbarkeiten',
                          'zentrale_orte', 'geom', key='id',
                          symbology=symbology, 
                          group=results_group)
        schema = 'erreichbarkeiten'
        def add_mat_view(mat_view, group): 
            rows = self.db_conn.fetch(
                'SELECT DISTINCT(search_time) from {s}.{v}'.format(
                s=schema, v=mat_view))
            times = sorted([r.search_time for r in rows])
            symbology = GraduatedSymbology('minuten', self.reach_color_ranges,
                                           no_pen=True)
            for time in times:
                layer_name = time.strftime("%H:%M")
                self.add_db_layer(layer_name, 'erreichbarkeiten',
                                  mat_view, 'geom', key='id', 
                                  symbology=symbology, group=group,
                                  where="search_time='{}'".format(time),
                                  visible=True)
            group.setIsMutuallyExclusive(
                True, initialChildIndex=max(len(times), 4))
            for child in group.children():
                child.setExpanded(False)
            
        mat_view = 'matview_err_ov_hin'
        group = get_group('Hinfahrt zu den zentralen Orten',
                          results_group)
        add_mat_view(mat_view, group)
        mat_view = 'matview_err_ov_zurueck'
        group = get_group(u'Rückfahrt von den zentralen Orten',
                          results_group)
        add_mat_view(mat_view, group)

    def get_selected_tab(self):
        '''
        return currently selected institution-tab
        '''
        idx = self.selection_tabs.currentIndex()
        tab_name = self.selection_tabs.tabText(idx)
        return tab_name
    
    def export_filter_layer(self, ext='xlsx'):
        '''
        export a filter-layer (opens user-input) to excel or kml
        '''
        try:
            res = self.get_filterlayer()
        except:
            traceback.print_exc()
            return
        if not res:
            return
        category, layer = res
        
        file_filter = EXCEL_FILTER if ext == 'xlsx' else KML_FILTER
        filepath = browse_file(None, 'Export', file_filter, save=True, 
                               parent=self)
        if not filepath:
            return
        driver = 'XLSX' if ext == 'xlsx'else 'KML'
        #fields = []
        #for i, f in enumerate(layer.fields()):
            #if layer.editorWidgetV2(i) == 'Hidden':
                #continue
            #fields.append(f.name())
        try:
            QgsVectorFileWriter.writeAsVectorFormat(
                layer, filepath, "utf-8", None, driver, False)
                #attributes=fields)
            title = 'Speicherung erfolgreich'
            msg = 'Die Daten wurden erfolgreich exportiert.'
        except Exception as e:
            title = 'Fehler'
            msg = 'Fehler bei der Speicherung: \n {}'.format(str(e))
        QtGui.QMessageBox.information(
            self, title, msg)
    
    def export_pdf(self, title=''):
        '''
        Export Composition (map view and checked layers) to PDF
        '''
        title = self.active_scenario.name if self.active_scenario else ''
        dialog = ExportPDFDialog(title=title, parent=self)
        result = dialog.exec_()
        ok = result == QtGui.QDialog.Accepted
        if not ok:
            return
        title = dialog.title
        date = dialog.date
        filepath = browse_file(None, 'Export', PDF_FILTER, save=True, 
                               parent=self)
        if not filepath:
            return
        bridge = QgsLayerTreeMapCanvasBridge(
            QgsProject.instance().layerTreeRoot(), self.canvas)
        bridge.setCanvasLayers()
    
        template_file = file(REPORT_TEMPLATE_PATH)
        template_content = template_file.read()
        template_file.close()
        document = QDomDocument()
        document.setContent(template_content)
        composition = QgsComposition(self.canvas.mapSettings())
        # You can use this to replace any string like this [key]
        # in the template with a new value. e.g. to replace
        # [date] pass a map like this {'date': '1 Jan 2012'}
        substitution_map = {
            'TITLE': title,
            'DATE_TIME': date}
        composition.loadFromTemplate(document, substitution_map)
        # You must set the id in the template
        map_item = composition.getComposerItemById('map')
        map_item.setMapCanvas(self.canvas)
        map_item.zoomToExtent(self.canvas.extent())
        # You must set the id in the template
        legend_item = composition.getComposerItemById('legend')
        legend_item.updateLegend()
        composition.refreshItems()
        composition.exportAsPDF(filepath)
        if sys.platform.startswith('darwin'):
            subprocess.call(('open', filepath))
        elif os.name == 'nt':
            os.startfile(filepath)
        elif os.name == 'posix':
            subprocess.call(('xdg-open', filepath))

    def get_layer_scenario_id(self, layer):
        provider = layer.dataProvider()
        uri = provider.dataSourceUri()
        regex='szenario_id\=(d+)'
        match=re.search(regex, uri)
        if not match:
            return None
        return match.group(1)

def get_group(groupname, parent_group=None, add_at_index=None,
              hide_in_composer=True):
    '''
    get a group from qgis-layer-window, if exists, else add it
    
    Parameters
    ----------
    groupname: name of group
    parent_group: optional, parent to look for group in, if not given root
    add_at_index: optional, add the layer to given index (relative to other siblings)
    hide_in_composer: optional, if True, hide group in legend, when exporting to pdf
    '''
    if not parent_group:
        parent_group = QgsProject.instance().layerTreeRoot()
    group = parent_group.findGroup(groupname)
    if not group:
        if add_at_index is not None:
            group = parent_group.insertGroup(add_at_index, groupname)
        else:
            group = parent_group.addGroup(groupname)
    if hide_in_composer:
        QgsLegendRenderer.setNodeLegendStyle(
            group, QgsComposerLegendStyle.Hidden)
    return group

def remove_group_layers(group):
    '''
    remove group and it's children from qgis-layer-window
    '''
    for child in group.children():
        if not hasattr(child, 'layer'):
            continue
        l = child.layer()
        QgsMapLayerRegistry.instance().removeMapLayer(l.id())

def remove_layer(name, group=None):
    '''
    remove layer from qgis-layer-window
    '''
    
    if not group:
        ex = QgsMapLayerRegistry.instance().mapLayersByName(name)
        if len(ex) > 0:
            for e in ex:
                QgsMapLayerRegistry.instance().removeMapLayer(e.id())
    else:
        for child in group.children():
            if not hasattr(child, 'layer'):
                continue
            l = child.layer()
            if l and l.name() == name:
                QgsMapLayerRegistry.instance().removeMapLayer(l.id())
        
def get_unique_layer_name(name, group):
    '''
    look for given layername in group,
    if it already exists a suffix is prepended to make it unique
    '''
    orig_name = name
    retry = True
    i = 2
    while retry:
        retry = False
        for child in group.children():
            if not hasattr(child, 'layer'):
                continue
            l = child.layer()
            if l and l.name() == name:
                name = orig_name + '_{}'.format(i)
                retry = True
                i += 1
                break
    return name

def show_help(parent, key, json_file, height=None):
    '''
    show a help-dialog with text from json_file,
    the entry is identified by given key
    '''
    with open(json_file) as f:
        data = f.read().replace('\n', '').replace('\r', '')
        text = json.loads(data)[key]
        dialog = HelpDialog(text, height=height, parent=parent)
        dialog.exec_()
        
if __name__ == '__main__':
    print
    
        
        
    